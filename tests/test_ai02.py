import asyncio
import os
import random
import re

os.environ.setdefault("AI_INTERNAL_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient

from app import llm, main, pipelines
from app.errors import AIError
from app.schemas import (
    QUESTION_OUTPUTS,
    EssayDraft,
    GenerateQuestionsRequest,
    MCDraft,
    OXDraft,
    ShortDraft,
)

QUOTE = "스택은 나중에 넣은 데이터가 먼저 나오는 구조다."
PAGES = [{"page": 3, "text": QUOTE + " 푸시와 팝 연산을 쓴다."}, {"page": 4, "text": "큐는 먼저 넣은 데이터가 먼저 나오는 구조다."}]


def request(*specs, exclude=()) -> GenerateQuestionsRequest:
    return GenerateQuestionsRequest(
        category={"categoryId": 7, "name": "스택과 큐", "pages": PAGES},
        requests=[{"type": t, "difficulty": d, "count": c} for t, d, c in specs],
        exclude_stems=list(exclude),
    )


def draft(qtype: str, i: int, **overrides):
    """서로 다른 지문의 통과 가능한 문제. i가 달라야 중복으로 걸리지 않는다."""
    # 서로 닮지 않은 지문이어야 중복 검사(유사도 0.75)에 걸리지 않는다
    word = "".join(chr(0xAC00 + (i * 7919 + k * 104729) % 11172) for k in range(12))
    base = {
        "stem": f"{word} 확인 문제",
        "explanation": "스택은 나중에 넣은 것이 먼저 나와요.",
        "source_page": 3,
        "evidence_quote": QUOTE,
        "concept_tags": ["스택"],
    }
    kinds = {
        "OX": lambda: OXDraft(answer="O", **base),
        "MC": lambda: MCDraft(correct_choice="스택", wrong_choices=["큐", "트리", "그래프"], **base),
        "SHORT": lambda: ShortDraft(answer="스택", accepted_answers=["Stack"], **base),
        "ESSAY": lambda: EssayDraft(rubric=["LIFO", "푸시", "팝"], **base),
    }
    made = kinds[qtype]()
    return made.model_copy(update=overrides)


@pytest.fixture
def fake(monkeypatch):
    """요청한 개수만큼 문제를 돌려주는 가짜 LLM. bad(호출 순번, 유형)이 참이면 그 호출의 문제는 근거가 틀린다."""

    def install(bad=lambda call, qtype: False, fail=lambda call, qtype: False):
        calls = []
        counter = iter(range(10_000))

        async def parse(feature, *, schema, input, usage, count_attempt=True, **kwargs):
            n = len(calls)
            qtype = next(t for t, s in QUESTION_OUTPUTS.items() if s is schema)
            count = int(re.search(r"개수: (\d+)", input)[1])
            calls.append({"feature": feature, "type": qtype, "count": count, "input": input, **kwargs})
            usage.attempts += 1
            if fail(n, qtype):
                raise AIError("LLM_FAILED")
            overrides = {"evidence_quote": "자료에 없는 문장을 근거라고 주장한다"} if bad(n, qtype) else {}
            return schema(questions=[draft(qtype, next(counter), **overrides) for _ in range(count)])

        monkeypatch.setattr(llm, "parse", parse)
        return calls

    return install


def run(req):
    return asyncio.run(pipelines.generate_questions(req, random.Random(0)))


def test_counts_types_and_difficulty_come_from_request(fake):
    calls = fake()
    out = run(request(("OX", 2, 3), ("MC", 3, 2)))
    assert [(q["type"], q["difficulty"]) for q in out["questions"]] == [("OX", 2)] * 3 + [("MC", 3)] * 2
    assert out["shortfall"] == []
    assert out["rejected"] == {"schema": 0, "evidence": 0, "duplicate": 0}
    assert sorted(c["count"] for c in calls) == [3, 4]  # 20% 여유분: ceil(3×1.2)=4, ceil(2×1.2)=3
    assert all(c["cache_key"] == "category-7" for c in calls)


def test_large_request_is_split_into_chunks(fake):
    calls = fake()
    out = run(request(("MC", 2, 15)))
    assert len(out["questions"]) == 15
    assert sorted(c["count"] for c in calls) == [8, 10]  # ceil(15*1.2)=18 → 10 + 8


def test_rejected_questions_are_regenerated(fake):
    calls = fake(bad=lambda call, qtype: call == 0)
    out = run(request(("OX", 1, 5)))
    assert len(out["questions"]) == 5
    assert out["rejected"]["evidence"] == 6
    assert len(calls) == 2


def test_regeneration_excludes_existing_stems(fake):
    calls = fake(bad=lambda call, qtype: call == 0)
    run(request(("OX", 1, 2), exclude=["이미 있는 문제 지문"]))
    assert "이미 있는 문제 지문" in calls[0]["input"]
    assert "이미 있는 문제 지문" in calls[1]["input"]


def test_persistent_failure_reports_shortfall(fake):
    calls = fake(bad=lambda call, qtype: qtype == "SHORT")
    out = run(request(("OX", 2, 2), ("SHORT", 2, 3)))
    assert [q["type"] for q in out["questions"]] == ["OX", "OX"]
    assert out["shortfall"] == [{"type": "SHORT", "difficulty": 2, "count": 3}]
    assert len([c for c in calls if c["type"] == "SHORT"]) == pipelines.AI02_ROUNDS


def test_one_type_failing_keeps_the_others(fake):
    fake(fail=lambda call, qtype: qtype == "MC")
    out = run(request(("OX", 2, 2), ("MC", 2, 2)))
    assert len(out["questions"]) == 2
    assert out["shortfall"] == [{"type": "MC", "difficulty": 2, "count": 2}]


def test_no_questions_at_all_is_llm_failed(fake):
    fake(fail=lambda call, qtype: True)
    with pytest.raises(AIError) as e:
        run(request(("OX", 2, 2)))
    assert e.value.code == "LLM_FAILED"


def test_mc_answer_points_to_correct_choice(fake):
    fake()
    for q in run(request(("MC", 2, 5)))["questions"]:
        assert q["choices"][int(q["answer"]) - 1] == "스택"


def test_route_uses_pipeline_when_mock_is_off(fake, monkeypatch):
    fake()
    monkeypatch.setattr(main, "MOCK", False)
    body = {
        "category": {"categoryId": 7, "name": "스택과 큐", "pages": PAGES},
        "requests": [{"type": "SHORT", "difficulty": 1, "count": 2}],
    }
    r = TestClient(main.app).post("/v1/questions/generate", json=body, headers={"X-Internal-Key": os.environ["AI_INTERNAL_KEY"]})
    assert r.status_code == 200
    out = r.json()
    assert len(out["questions"]) == 2
    assert out["questions"][0]["acceptedAnswers"] == ["스택", "Stack"]
    assert set(out["meta"]) == {"model", "inputTokens", "outputTokens", "elapsedMs", "attempts"}

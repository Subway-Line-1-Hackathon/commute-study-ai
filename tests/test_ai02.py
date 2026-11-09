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
    PlanOut,
    Point,
    ShortDraft,
)

QUOTE = "스택은 나중에 넣은 데이터가 먼저 나오는 구조다."
QUEUE = "큐는 먼저 넣은 데이터가 먼저 나오는 구조다."
PAGES = [{"page": 3, "text": QUOTE + " 푸시와 팝 연산을 쓴다."}, {"page": 4, "text": QUEUE}]


def request(*specs, exclude=()) -> GenerateQuestionsRequest:
    return GenerateQuestionsRequest(
        category={"categoryId": 7, "name": "스택과 큐", "pages": PAGES},
        requests=[{"type": t, "difficulty": d, "count": c} for t, d, c in specs],
        exclude_stems=list(exclude),
    )


def points(n: int, importance="core", types=("OX", "MC", "SHORT"), start=0) -> list[Point]:
    """서로 닮지 않은 포인트 n개. 근거는 자료의 두 문장을 번갈아 쓴다."""
    return [
        Point(
            statement=_word(1000 + start + i) + " 사실",
            concept=_word(1000 + start + i),
            source_page=3 if i % 2 == 0 else 4,
            evidence_quote=QUOTE if i % 2 == 0 else QUEUE,
            importance=importance,
            types=list(types),
        )
        for i in range(n)
    ]


def _word(i: int) -> str:
    # 서로 닮지 않은 글자열이어야 중복 검사(유사도 0.75)에 걸리지 않는다
    return "".join(chr(0xAC00 + (i * 7919 + k * 104729) % 11172) for k in range(12))


def draft(qtype: str, i: int, point: int, **overrides):
    """통과 가능한 문제. i가 달라야 중복으로 걸리지 않는다."""
    base = {"point": point, "stem": f"{_word(i)} 확인 문제", "explanation": "스택은 나중에 넣은 것이 먼저 나와요.", "concept_tags": ["스택"]}
    kinds = {
        "OX": lambda: OXDraft(answer="O", **base),
        "MC": lambda: MCDraft(correct_choice="스택", wrong_choices=["큐", "트리", "그래프"], **base),
        "SHORT": lambda: ShortDraft(answer="스택", accepted_answers=["Stack"], **base),
        "ESSAY": lambda: EssayDraft(rubric=["LIFO", "푸시", "팝"], **base),
    }
    return kinds[qtype]().model_copy(update=overrides)


@pytest.fixture
def fake(monkeypatch):
    """가짜 LLM. 출제 계획에는 plan을 돌려주고, 출제에는 입력에 적힌 포인트마다 문제 하나를 돌려준다.

    bad(호출 순번, 유형)이 참이면 그 호출의 문제는 지문이 비어 탈락한다. fail이 참이면 그 호출은 실패한다.
    """

    def install(plan=None, bad=lambda call, qtype: False, fail=lambda call, qtype: False):
        calls = []
        counter = iter(range(10_000))

        async def parse(feature, *, schema, input, usage, count_attempt=True, **kwargs):
            usage.attempts += 1
            if schema is PlanOut:
                calls.append({"type": "PLAN", "input": input, **kwargs})
                return PlanOut(points=points(6) if plan is None else plan)
            n = len([c for c in calls if c["type"] != "PLAN"])
            qtype = next(t for t, s in QUESTION_OUTPUTS.items() if s is schema)
            nums = [int(m) for m in re.findall(r"^(\d+)\. ", input, re.M)]
            calls.append({"type": qtype, "points": nums, "input": input, **kwargs})
            if fail(n, qtype):
                raise AIError("LLM_FAILED")
            overrides = {"stem": " "} if bad(n, qtype) else {}
            return schema(questions=[draft(qtype, next(counter), p, **overrides) for p in nums])

        monkeypatch.setattr(llm, "parse", parse)
        return calls

    return install


def run(req):
    return asyncio.run(pipelines.generate_questions(req, random.Random(0)))


def gen_calls(calls):
    return [c for c in calls if c["type"] != "PLAN"]


def test_plan_then_one_question_per_assigned_point(fake):
    calls = fake()
    out = run(request(("OX", 2, 3), ("MC", 3, 2)))
    assert [(q["type"], q["difficulty"]) for q in out["questions"]] == [("OX", 2)] * 3 + [("MC", 3)] * 2
    assert out["shortfall"] == []
    assert out["capacity"] == 12  # core 6개 × 2문제
    assert out["rejected"] == {"schema": 0, "evidence": 0, "duplicate": 0}
    assert calls[0]["type"] == "PLAN"
    assert sorted(len(c["points"]) for c in gen_calls(calls)) == [2, 3]
    assert all(c["cache_key"] == "category-7" for c in calls)
    assert all("point" not in q for q in out["questions"])


def test_page_and_evidence_come_from_the_point(fake):
    fake()
    for q in run(request(("OX", 2, 6)))["questions"]:
        assert (q["sourcePage"], q["evidenceQuote"]) in {(3, QUOTE), (4, QUEUE)}


def test_small_material_returns_capacity_and_shortfall(fake):
    fake(plan=points(2) + points(3, importance="minor", types=("OX",), start=2))
    out = run(request(("OX", 2, 14), ("MC", 2, 13), ("SHORT", 2, 13)))
    assert out["capacity"] == 2 * 2 + 3
    assert len(out["questions"]) == 7
    assert sum(s["count"] for s in out["shortfall"]) == 40 - 7


def test_large_assignment_is_split_into_chunks(fake):
    calls = fake(plan=points(20))
    out = run(request(("MC", 2, 15)))
    assert len(out["questions"]) == 15
    assert sorted(len(c["points"]) for c in gen_calls(calls)) == [5, 10]


def test_only_rejected_points_are_regenerated(fake):
    calls = fake(bad=lambda call, qtype: call == 0)
    out = run(request(("OX", 1, 5)))
    assert len(out["questions"]) == 5
    assert out["rejected"]["schema"] == 5
    assert [len(c["points"]) for c in gen_calls(calls)] == [5, 5]


def test_regeneration_excludes_existing_stems(fake):
    calls = fake(bad=lambda call, qtype: call == 0)
    run(request(("OX", 1, 2), exclude=["이미 있는 문제 지문"]))
    assert all("이미 있는 문제 지문" in c["input"] for c in gen_calls(calls))


def test_additional_generation_stops_at_capacity(fake):
    calls = fake()
    out = run(request(("OX", 2, 5), exclude=[f"기존 {i}" for i in range(6)]))
    assert out["questions"] == []
    assert out["capacity"] == 6  # core 6개 × O/X
    assert out["shortfall"] == [{"type": "OX", "difficulty": 2, "count": 5}]
    assert gen_calls(calls) == []


def test_capacity_counts_only_what_the_requested_types_can_use(fake):
    fake(plan=points(4, types=("OX",)))
    out = run(request(("OX", 2, 2), ("SHORT", 2, 3)))
    assert out["capacity"] == 4  # 단답으로 낼 포인트가 없다
    assert [q["type"] for q in out["questions"]] == ["OX"] * 4  # 단답 몫을 O/X가 채운다
    assert out["shortfall"] == [{"type": "SHORT", "difficulty": 2, "count": 3}]


def test_nothing_assignable_is_empty_not_failed(fake):
    calls = fake(plan=points(4, types=("OX",)))
    out = run(request(("SHORT", 2, 3)))
    assert (out["questions"], out["capacity"]) == ([], 0)
    assert gen_calls(calls) == []


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


def test_plan_without_valid_points_is_llm_failed(fake):
    bad = points(3)
    for p in bad:
        p.evidence_quote = "자료에 없는 문장을 근거라고 주장한다"
    fake(plan=bad)
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

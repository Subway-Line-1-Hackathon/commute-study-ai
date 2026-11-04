import asyncio
import os

os.environ.setdefault("AI_INTERNAL_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient

from app import llm, main, pipelines
from app.errors import AIError
from app.schemas import FeedbackOut, FeedbackRequest, JudgeOut, JudgeShortRequest

KEY = {"X-Internal-Key": os.environ["AI_INTERNAL_KEY"]}


@pytest.fixture
def fake(monkeypatch):
    """llm.parse 대신 정해진 답(또는 예외)을 주고, 보낸 내용을 기록한다."""

    def install(result):
        calls = []

        async def parse(feature, **kwargs):
            calls.append({"feature": feature, **kwargs})
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(llm, "parse", parse)
        return calls

    return install


# ---------- AI-03 ----------


def judge(user_answer, accepted=("큐", "Queue")):
    req = JudgeShortRequest(stem="먼저 넣은 것이 먼저 나오는 구조는?", accepted_answers=list(accepted), explanation="큐는 FIFO다.", user_answer=user_answer)
    return asyncio.run(pipelines.judge_short(req))


@pytest.mark.parametrize("answer", ["queue", " QUEUE ", "큐.", "Q u e u e"])
def test_normalized_match_skips_llm(fake, answer):
    calls = fake(JudgeOut(correct=False, reason="불리면 안 됨"))
    assert judge(answer)["correct"] is True
    assert calls == []


def test_empty_answer_is_wrong_without_llm(fake):
    calls = fake(JudgeOut(correct=True, reason="불리면 안 됨"))
    assert judge("   ") == {"correct": False, "reason": "답을 입력하지 않았어요."}
    assert calls == []


def test_llm_judges_different_wording(fake):
    calls = fake(JudgeOut(correct=True, reason=" 큐를 뜻하는 다른 표현이에요. "))
    assert judge("대기열") == {"correct": True, "reason": "큐를 뜻하는 다른 표현이에요."}
    sent = calls[0]
    assert sent["feature"] == "AI03"
    assert "<user_answer>대기열</user_answer>" in sent["input"]
    assert "큐, Queue" in sent["input"]
    assert 0 < sent["deadline"].remaining() <= pipelines.AI03_SECONDS


def test_long_answer_is_truncated(fake):
    calls = fake(JudgeOut(correct=False, reason="아니에요"))
    judge("가" * 500)
    assert "가" * pipelines.AI03_MAX_ANSWER + "</user_answer>" in calls[0]["input"]


def test_judge_errors_pass_through(fake):
    fake(AIError("LLM_TIMEOUT"))
    with pytest.raises(AIError) as e:
        judge("대기열")
    assert e.value.code == "LLM_TIMEOUT"


# ---------- AI-06 ----------


def feedback_request(**overrides) -> FeedbackRequest:
    body = {
        "session_id": 1,
        "mode": "CURRICULUM",
        "total": 15,
        "answered": 15,
        "correct": 12,
        "correct_concepts": ["스택", "큐", "스택", "LIFO", "트리"],
        "wrong_items": [
            {"stem": "FIFO 구조는?", "concept_tags": ["FIFO", "큐"]},
            {"stem": "덱이란?", "concept_tags": ["덱", "FIFO"]},
        ],
    }
    return FeedbackRequest(**{**body, **overrides})


def feedback(req):
    return asyncio.run(pipelines.session_feedback(req))


def test_feedback_sends_aggregated_concepts(fake):
    calls = fake(FeedbackOut(feedback=" 잘했어요. "))
    assert feedback(feedback_request()) == {"feedback": "잘했어요."}
    sent = calls[0]["input"]
    assert "헷갈린 개념: FIFO, 큐, 덱" in sent
    # 틀린 개념(큐)은 잘한 개념에서 뺀다
    assert "잘한 개념: 스택, LIFO, 트리" in sent
    assert "문제 15개 중 15개를 풀고 12개를 맞혔다." in sent
    assert "- FIFO 구조는?" in sent
    assert calls[0]["feature"] == "AI06"


def test_review_mode_and_empty_lists(fake):
    calls = fake(FeedbackOut(feedback="좋아요"))
    feedback(feedback_request(mode="REVIEW", correct_concepts=[], wrong_items=[]))
    sent = calls[0]["input"]
    assert "모드: 복습" in sent
    assert "헷갈린 개념: (없음)" in sent
    assert "틀린 문제:\n(없음)" in sent


def test_wrong_items_are_capped(fake):
    calls = fake(FeedbackOut(feedback="좋아요"))
    items = [{"stem": f"{i}번 " + "가" * 300, "concept_tags": []} for i in range(30)]
    feedback(feedback_request(wrong_items=items))
    lines = [line for line in calls[0]["input"].splitlines() if line.startswith("- ")]
    assert len(lines) == pipelines.AI06_MAX_WRONG_ITEMS
    assert all(len(line) <= pipelines.AI06_MAX_STEM + 2 for line in lines)


def test_no_answered_questions_is_invalid(fake):
    calls = fake(FeedbackOut(feedback="불리면 안 됨"))
    with pytest.raises(AIError) as e:
        feedback(feedback_request(answered=0, correct=0))
    assert e.value.code == "INVALID_REQUEST"
    assert calls == []


# ---------- API 연결 ----------


def test_routes_use_pipelines_when_mock_is_off(fake, monkeypatch):
    monkeypatch.setattr(main, "MOCK", False)
    client = TestClient(main.app)

    fake(JudgeOut(correct=False, reason="정답은 큐예요."))
    body = {"stem": "s", "acceptedAnswers": ["큐"], "explanation": "e", "userAnswer": "스택"}
    assert client.post("/v1/answers/judge-short", json=body, headers=KEY).json() == {"correct": False, "reason": "정답은 큐예요."}

    fake(FeedbackOut(feedback="잘했어요."))
    body = {"sessionId": 1, "mode": "CURRICULUM", "total": 3, "answered": 3, "correct": 3}
    assert client.post("/v1/sessions/feedback", json=body, headers=KEY).json() == {"feedback": "잘했어요."}

    fake(AIError("LLM_TIMEOUT"))
    r = client.post("/v1/sessions/feedback", json=body, headers=KEY)
    assert (r.status_code, r.json()["error"]["code"]) == (504, "LLM_TIMEOUT")

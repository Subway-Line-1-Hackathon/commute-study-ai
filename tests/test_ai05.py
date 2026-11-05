import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("AI_INTERNAL_KEY", "test-key")

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient

from app import llm, main, pipelines
from app.errors import AIError
from app.schemas import TutorAskRequest

KEY = {"X-Internal-Key": os.environ["AI_INTERNAL_KEY"]}


# ---------- llm.stream ----------


def delta(text):
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def end(status="completed", tokens=(500, 40)):
    usage = SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1])
    return SimpleNamespace(type=f"response.{status}", response=SimpleNamespace(status=status, usage=usage))


class FakeStream:
    def __init__(self, events, error=None):
        self.events, self.error, self.closed = events, error, False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for e in self.events:
            yield e
        if self.error:
            raise self.error

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_openai(monkeypatch):
    def install(events, error=None):
        stream = FakeStream(events, error)
        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            return stream

        monkeypatch.setattr(llm, "_client", SimpleNamespace(responses=SimpleNamespace(create=create)))
        return stream, calls

    return install


def collect(usage=None, deadline=None):
    usage = usage or llm.Usage(model="m")

    async def run():
        out = []
        async for d in llm.stream(
            "AI05", instructions="i", input="q", deadline=deadline or llm.Deadline(5), usage=usage, max_output_tokens=600, cache_key="category-1"
        ):
            out.append(d)
        return out

    return asyncio.run(run())


def stream_error(**kwargs) -> str:
    with pytest.raises(AIError) as e:
        collect(**kwargs)
    return e.value.code


def test_stream_yields_deltas_and_counts_usage(fake_openai):
    stream, calls = fake_openai([delta("큐는 "), delta("FIFO예요."), end()])
    usage = llm.Usage(model="m")
    assert collect(usage) == ["큐는 ", "FIFO예요."]
    assert (usage.input_tokens, usage.output_tokens, usage.attempts) == (500, 40, 1)
    assert calls[0]["stream"] is True
    assert calls[0]["prompt_cache_key"] == "category-1"
    assert stream.closed


def test_truncated_answer_is_failure(fake_openai):
    fake_openai([delta("큐는"), end("incomplete")])
    assert stream_error() == "LLM_FAILED"


def test_no_text_is_failure(fake_openai):
    fake_openai([end()])
    assert stream_error() == "LLM_FAILED"


def test_error_event_is_failure(fake_openai):
    fake_openai([delta("큐는"), SimpleNamespace(type="error", message="boom")])
    assert stream_error() == "LLM_FAILED"


def test_timeout_mid_stream(fake_openai):
    stream, _ = fake_openai([delta("큐는")], error=openai.APITimeoutError(request=httpx2.Request("POST", "https://x")))
    assert stream_error() == "LLM_TIMEOUT"
    assert stream.closed


def test_deadline_passed_mid_stream(fake_openai, monkeypatch):
    fake_openai([delta("a"), delta("b"), end()])
    deadline = llm.Deadline(5)
    monkeypatch.setattr(deadline, "remaining", lambda: 0.0)
    assert stream_error(deadline=deadline) == "LLM_TIMEOUT"


# ---------- 프롬프트 구성 ----------


def tutor_request(**overrides) -> TutorAskRequest:
    body = {
        "category": {"categoryId": 7, "name": "스택과 큐", "pages": [{"page": 2, "text": "큐는 FIFO 구조다."}]},
        "question": {"stem": "FIFO 구조는?", "choices": ["스택", "큐"], "answer": "2", "explanation": "큐는 FIFO다.", "userAnswer": "1"},
        "history": [{"role": "user", "content": "왜요?"}, {"role": "assistant", "content": "가" * 2000}],
        "message": "예시를 들어 주세요",
    }
    return TutorAskRequest(**{**body, **overrides})


def test_tutor_messages(monkeypatch):
    sent = {}

    async def fake_stream(feature, **kwargs):
        sent.update(kwargs, feature=feature)
        yield "답"

    monkeypatch.setattr(llm, "stream", fake_stream)

    async def run():
        return [d async for d in pipelines.tutor_ask(tutor_request(), llm.Usage(model="m"))]

    assert asyncio.run(run()) == ["답"]
    first, *history, last = sent["input"]
    assert first["content"].startswith("단원: 스택과 큐\n<document>")
    assert '<page n="2">' in first["content"]
    assert "1. 스택\n2. 큐" in first["content"]
    assert "학생 답: 1" in first["content"]
    assert [t["role"] for t in history] == ["user", "assistant"]
    assert len(history[1]["content"]) == pipelines.AI05_MAX_TURN
    assert last == {"role": "user", "content": "예시를 들어 주세요"}
    assert sent["cache_key"] == "category-7"
    assert sent["feature"] == "AI05"


# ---------- SSE 응답 ----------

BODY = {
    "category": {"categoryId": 1, "name": "c", "pages": [{"page": 1, "text": "t"}]},
    "question": {"stem": "s", "answer": "O", "explanation": "e"},
    "message": "왜요?",
}


def events_of(text: str) -> list[tuple[str, str]]:
    blocks = [b for b in text.split("\n\n") if b]
    return [tuple(line.split(": ", 1)[1] for line in b.split("\n")) for b in blocks]


def post(monkeypatch, fake_tutor):
    monkeypatch.setattr(main, "MOCK", False)
    monkeypatch.setattr(pipelines, "tutor_ask", fake_tutor)
    r = TestClient(main.app).post("/v1/tutor/ask", json=BODY, headers=KEY)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    return events_of(r.text)


def test_sse_delta_then_done(monkeypatch):
    async def fake_tutor(req, usage):
        usage.attempts += 1
        yield "큐는 "
        yield "FIFO예요."

    events = post(monkeypatch, fake_tutor)
    assert [e for e, _ in events] == ["delta", "delta", "done"]
    assert events[0][1] == '{"text": "큐는 "}'
    assert '"attempts": 1' in events[2][1]


def test_sse_error_after_delta(monkeypatch):
    async def fake_tutor(req, usage):
        yield "큐는 "
        raise AIError("LLM_TIMEOUT")

    events = post(monkeypatch, fake_tutor)
    assert [e for e, _ in events] == ["delta", "error"]
    assert '"code": "LLM_TIMEOUT"' in events[1][1]


def test_sse_unexpected_error_becomes_llm_failed(monkeypatch):
    async def fake_tutor(req, usage):
        raise RuntimeError("bug")
        yield

    events = post(monkeypatch, fake_tutor)
    assert [e for e, _ in events] == ["error"]
    assert '"code": "LLM_FAILED"' in events[0][1]


def test_invalid_request_is_json_400(monkeypatch):
    monkeypatch.setattr(main, "MOCK", False)
    r = TestClient(main.app).post("/v1/tutor/ask", json={**BODY, "message": ""}, headers=KEY)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "INVALID_REQUEST")

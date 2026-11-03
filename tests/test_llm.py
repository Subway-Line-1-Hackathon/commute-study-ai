import asyncio
from types import SimpleNamespace

import httpx2
import openai
import pytest
from pydantic import BaseModel

from app import llm
from app.errors import AIError
from app.llm import Deadline, Usage

REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


class Answer(BaseModel):
    correct: bool


class FakeResponses:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def response(status="completed", parsed=Answer(correct=True), tokens=(100, 20)):
    return SimpleNamespace(
        status=status,
        output_parsed=parsed,
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


@pytest.fixture
def fake(monkeypatch):
    def install(**kwargs):
        responses = FakeResponses(**kwargs)
        monkeypatch.setattr(llm, "_client", SimpleNamespace(responses=responses))
        return responses

    return install


def call(usage=None, deadline=None, **kwargs):
    return asyncio.run(
        llm.parse(
            "AI03",
            instructions="판정",
            input="답안",
            schema=Answer,
            deadline=deadline or Deadline(5),
            usage=usage or Usage(model="m"),
            max_output_tokens=150,
            **kwargs,
        )
    )


def error_code(**kwargs) -> str:
    with pytest.raises(AIError) as e:
        call(**kwargs)
    return e.value.code


def test_success_returns_parsed_and_counts_usage(fake):
    responses = fake(result=response())
    usage = Usage(model="m")
    assert call(usage=usage) == Answer(correct=True)
    assert (usage.input_tokens, usage.output_tokens, usage.attempts) == (100, 20, 1)

    sent = responses.calls[0]
    assert sent["model"] == llm.MODELS["AI03"]
    assert sent["reasoning"] == {"effort": llm.EFFORTS["AI03"]}
    assert 0 < sent["timeout"] <= 5
    assert "prompt_cache_key" not in sent


def test_cache_key_is_sent_when_given(fake):
    responses = fake(result=response())
    call(cache_key="category-1")
    assert responses.calls[0]["prompt_cache_key"] == "category-1"


def test_expired_deadline_skips_the_call(fake):
    responses = fake(result=response())
    assert error_code(deadline=Deadline(0)) == "LLM_TIMEOUT"
    assert responses.calls == []


def test_timeout_maps_to_llm_timeout(fake):
    fake(error=openai.APITimeoutError(request=REQUEST))
    assert error_code() == "LLM_TIMEOUT"


@pytest.mark.parametrize(
    "error",
    [openai.APIConnectionError(request=REQUEST), ValueError("truncated json")],
)
def test_api_and_parse_errors_map_to_llm_failed(fake, error):
    fake(error=error)
    assert error_code() == "LLM_FAILED"


@pytest.mark.parametrize("result", [response(status="incomplete"), response(parsed=None)])
def test_incomplete_or_refusal_is_failure_but_tokens_count(fake, result):
    fake(result=result)
    usage = Usage(model="m")
    assert error_code(usage=usage) == "LLM_FAILED"
    assert usage.input_tokens == 100


def test_helper_calls_add_tokens_without_attempts(fake):
    fake(result=response())
    usage = Usage(model="m")
    call(usage=usage, count_attempt=False)
    assert (usage.attempts, usage.input_tokens) == (0, 100)


def test_meta_matches_api_spec():
    assert set(Usage(model="m").meta()) == {"model", "inputTokens", "outputTokens", "elapsedMs", "attempts"}

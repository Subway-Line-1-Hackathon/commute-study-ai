"""OpenAI 호출.

SDK 자동 재시도는 끈다. 재시도 횟수와 시간 상한은 각 기능이 API 명세대로 정한다.
"""

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.errors import AIError

log = logging.getLogger("llm")

# 기능별 시작값. 1단계 검증 결과로 확정하고, 환경변수 MODEL_AI01 등으로 바꿀 수 있다.
# AI02는 V3에서 luna·terra·sol을 비교해 정한다.
_DEFAULTS = {
    "OCR": ("gpt-6-luna", "none"),
    "AI01": ("gpt-6.1-terra", "low"),
    "AI02": ("gpt-6.1-terra", "low"),
    "AI03": ("gpt-6-luna", "none"),
    "AI04": ("gpt-6.1-terra", "low"),
    "AI05": ("gpt-6.1-terra", "none"),
    "AI06": ("gpt-6-luna", "none"),
}
MODELS = {f: os.environ.get(f"MODEL_{f}", m) for f, (m, _) in _DEFAULTS.items()}
EFFORTS = {f: os.environ.get(f"EFFORT_{f}", e) for f, (_, e) in _DEFAULTS.items()}

RATE_LIMIT_WAITS = 3  # 분당 한도에 걸렸을 때 기다렸다 다시 보내는 최대 횟수
MAX_RATE_LIMIT_WAIT = 30.0

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(max_retries=0)  # OPENAI_API_KEY 환경변수를 읽는다
    return _client


class Deadline:
    """요청 하나의 마감 시각. 호출할 때마다 남은 시간을 타임아웃으로 쓴다."""

    def __init__(self, seconds: float):
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())


@dataclass
class Usage:
    """응답 meta. OCR처럼 보조 호출은 토큰만 더하고 attempts에는 세지 않는다."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 0
    started: float = field(default_factory=time.monotonic)

    def meta(self) -> dict:
        return {
            "model": self.model,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "elapsedMs": int((time.monotonic() - self.started) * 1000),
            "attempts": self.attempts,
        }


async def parse[T: BaseModel](
    feature: str,
    *,
    instructions: str,
    input: str | list,
    schema: type[T],
    deadline: Deadline,
    usage: Usage,
    max_output_tokens: int,
    cache_key: str | None = None,
    count_attempt: bool = True,
) -> T:
    """구조화 출력 호출 1회. 실패는 LLM_FAILED, 시간 초과는 LLM_TIMEOUT으로 바꾼다."""
    if deadline.remaining() <= 0:
        raise AIError("LLM_TIMEOUT")
    if count_attempt:
        usage.attempts += 1

    model = MODELS[feature]
    extra = {"prompt_cache_key": cache_key} if cache_key else {}
    started = time.monotonic()
    r = await _send(
        feature,
        model,
        started,
        deadline,
        lambda: client().responses.parse(
            model=model,
            instructions=instructions,
            input=input,
            text_format=schema,
            reasoning={"effort": EFFORTS[feature]},
            max_output_tokens=max_output_tokens,
            timeout=deadline.remaining(),
            **extra,
        ),
    )

    if r.usage:
        usage.input_tokens += r.usage.input_tokens
        usage.output_tokens += r.usage.output_tokens
    # incomplete는 출력 한도 초과, output_parsed가 없으면 거절(refusal)이다.
    if r.status != "completed" or r.output_parsed is None:
        _log(feature, model, started, f"LLM_FAILED status={r.status}", r.usage)
        raise AIError("LLM_FAILED")
    _log(feature, model, started, "ok", r.usage)
    return r.output_parsed


async def stream(
    feature: str,
    *,
    instructions: str,
    input: str | list,
    deadline: Deadline,
    usage: Usage,
    max_output_tokens: int,
    cache_key: str | None = None,
) -> AsyncIterator[str]:
    """스트리밍 호출 1회. 글자 조각을 내주고, 끝까지 받으면 usage에 토큰을 더한다.

    중간 실패는 AIError로 바뀐다. 이미 내준 조각은 되돌릴 수 없으므로 재시도하지 않는다.
    """
    if deadline.remaining() <= 0:
        raise AIError("LLM_TIMEOUT")
    usage.attempts += 1
    model = MODELS[feature]
    extra = {"prompt_cache_key": cache_key} if cache_key else {}
    started = time.monotonic()
    events = await _send(
        feature,
        model,
        started,
        deadline,
        lambda: client().responses.create(
            model=model,
            instructions=instructions,
            input=input,
            reasoning={"effort": EFFORTS[feature]},
            max_output_tokens=max_output_tokens,
            stream=True,
            timeout=deadline.remaining(),
            **extra,
        ),
    )
    final = None
    wrote = False
    try:
        async for e in events:
            if e.type == "response.output_text.delta":
                wrote = True
                yield e.delta
            elif e.type in ("response.completed", "response.incomplete", "response.failed"):
                final = e.response
            elif e.type == "error":
                break
            # ponytail: 마감은 조각이 올 때마다 확인한다. 조각 사이가 멈추면 SDK 타임아웃이 끊는다.
            if deadline.remaining() <= 0:
                _log(feature, model, started, "LLM_TIMEOUT")
                raise AIError("LLM_TIMEOUT")
    except openai.APITimeoutError:
        _log(feature, model, started, "LLM_TIMEOUT")
        raise AIError("LLM_TIMEOUT") from None
    except openai.OpenAIError as e:
        _log(feature, model, started, f"LLM_FAILED {type(e).__name__}")
        raise AIError("LLM_FAILED") from None
    finally:
        await events.close()

    u = final.usage if final else None
    if u:
        usage.input_tokens += u.input_tokens
        usage.output_tokens += u.output_tokens
    # incomplete는 출력 한도에서 잘린 답이고, 글자가 하나도 없으면 거절이다.
    if final is None or final.status != "completed" or not wrote:
        _log(feature, model, started, f"LLM_FAILED status={final.status if final else None}", u)
        raise AIError("LLM_FAILED")
    _log(feature, model, started, "ok", u)


async def _send(feature: str, model: str, started: float, deadline: Deadline, request: Callable[[], Awaitable]):
    """요청을 보낸다. 분당 한도에 걸리면 알려준 시간만큼 기다렸다 다시 보낸다."""
    for waits in range(RATE_LIMIT_WAITS + 1):
        try:
            return await request()
        except openai.RateLimitError as e:
            # 요청 하나가 한도보다 크거나, 하루 한도처럼 몇십 분을 기다리라고 하면 기다려도 소용없다.
            delay = _retry_after(e)
            too_long = delay > MAX_RATE_LIMIT_WAIT or delay >= deadline.remaining()
            if "Request too large" in str(e) or waits == RATE_LIMIT_WAITS or too_long:
                _log(feature, model, started, "LLM_FAILED RateLimitError")
                raise AIError("LLM_FAILED") from None
            _log(feature, model, started, f"rate limited, wait {delay:.1f}s")
            await asyncio.sleep(delay)
        except openai.APITimeoutError:
            _log(feature, model, started, "LLM_TIMEOUT")
            raise AIError("LLM_TIMEOUT") from None
        except (openai.OpenAIError, ValueError) as e:  # ValueError: 잘린 JSON 등 파싱 실패
            _log(feature, model, started, f"LLM_FAILED {type(e).__name__}")
            raise AIError("LLM_FAILED") from None


def _retry_after(e: openai.RateLimitError) -> float:
    """기다릴 초. 헤더가 없으면 메시지의 "try again in 6s" 같은 문구를 읽는다."""
    headers = e.response.headers
    if ms := headers.get("retry-after-ms"):
        delay = float(ms) / 1000
    elif sec := headers.get("retry-after"):
        delay = float(sec)
    elif m := re.search(r"try again in ([\d.]+)(ms|s)", str(e)):
        delay = float(m[1]) / (1000 if m[2] == "ms" else 1)
    else:
        delay = 2.0
    return delay + 0.2


def _log(feature: str, model: str, started: float, result: str, u=None):
    # 자료 본문과 답안은 남기지 않는다.
    tokens = f" in={u.input_tokens} out={u.output_tokens}" if u else ""
    log.info("%s %s %dms%s %s", feature, model, (time.monotonic() - started) * 1000, tokens, result)

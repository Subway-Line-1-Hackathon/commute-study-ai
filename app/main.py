import asyncio
import hmac
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from app import pipelines
from app.errors import ERRORS, AIError
from app.schemas import (
    FeedbackRequest,
    GenerateQuestionsRequest,
    GradeEssayRequest,
    JudgeShortRequest,
    ProcessMaterialRequest,
    TutorAskRequest,
)

INTERNAL_KEY = os.environ["AI_INTERNAL_KEY"]
FIXTURES = Path(__file__).parent.parent / "fixtures"
# 기본은 mock이다. 실제 AI를 쓰려면 AI_MOCK=false. 아직 구현하지 않은 기능은 계속 mock 응답을 준다.
MOCK = os.environ.get("AI_MOCK", "true").lower() != "false"


def require_key(x_internal_key: str = Header(default="")):
    if not hmac.compare_digest(x_internal_key.encode(), INTERNAL_KEY.encode()):
        raise AIError("UNAUTHORIZED")


def mock_error(x_mock_error: str | None = Header(default=None)) -> AIError | None:
    """mock 전용. 백엔드가 실패 처리를 시험할 수 있게 헤더로 에러를 낸다."""
    if x_mock_error is None:
        return None
    if x_mock_error not in ERRORS:
        raise AIError("INVALID_REQUEST", f"X-Mock-Error 값이 올바르지 않아요: {x_mock_error}")
    return AIError(x_mock_error)


def raise_mock_error(error: AIError | None = Depends(mock_error)):
    if error and MOCK:
        raise error


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


app = FastAPI(title="commute-study-ai")
v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_key)])
fails = [Depends(raise_mock_error)]


@app.exception_handler(AIError)
async def handle_ai_error(_, e: AIError):
    return JSONResponse(e.body(), status_code=e.status)


@app.exception_handler(RequestValidationError)
async def handle_invalid_request(_, e: RequestValidationError):
    first = e.errors()[0]
    where = ".".join(str(p) for p in first["loc"])
    return JSONResponse(AIError("INVALID_REQUEST", f"{where}: {first['msg']}").body(), status_code=400)


@app.get("/health")
def health():
    # Railway 헬스체크가 헤더를 보낼 수 없어서 내부 키를 요구하지 않는다.
    return {"status": "ok"}


@v1.post("/materials/process", dependencies=fails)
async def process_material(req: ProcessMaterialRequest):
    if MOCK:
        return fixture("materials_process")
    return await pipelines.process_material(req)


@v1.post("/questions/generate", dependencies=fails)
async def generate_questions(req: GenerateQuestionsRequest):
    if not MOCK:
        return await pipelines.generate_questions(req)
    data = fixture("questions_generate")
    specs = [(s.type, s.difficulty) for s in req.requests for _ in range(s.count)]
    questions = [
        {**data["samples"][t], "type": t, "difficulty": d, "stem": f"[mock {i}] {data['samples'][t]['stem']}"}
        for i, (t, d) in enumerate(specs, 1)
    ]
    return {"questions": questions, "shortfall": [], "rejected": data["rejected"], "meta": data["meta"]}


@v1.post("/answers/judge-short", dependencies=fails)
async def judge_short(req: JudgeShortRequest):
    if MOCK:
        return fixture("judge_short")
    return await pipelines.judge_short(req)


@v1.post("/answers/grade-essay", dependencies=fails)
def grade_essay(req: GradeEssayRequest):
    return fixture("grade_essay")


@v1.post("/tutor/ask")
def tutor_ask(req: TutorAskRequest, error: AIError | None = Depends(mock_error)):
    data = fixture("tutor_ask")
    chunks = re.findall(r"\S+\s*", data["text"])
    error = error if MOCK else None

    async def events():
        # 실제 스트림처럼 조금씩 보낸다. 에러를 시험할 때는 첫 조각 뒤에 끊는다.
        for chunk in chunks[:1] if error else chunks:
            yield sse("delta", {"text": chunk})
            await asyncio.sleep(0.05)
        yield sse("error", error.body()) if error else sse("done", {"meta": data["meta"]})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@v1.post("/sessions/feedback", dependencies=fails)
async def session_feedback(req: FeedbackRequest):
    if MOCK:
        return fixture("sessions_feedback")
    return await pipelines.session_feedback(req)


app.include_router(v1)

"""기능별 처리 흐름. 입출력은 docs/API_명세.md, 단계는 docs/기능별_개발계획.md를 따른다."""

import asyncio
import base64
import math
from collections import Counter
import os
import random
from pathlib import Path
from urllib.parse import urlparse

import httpx2

from app import extract, llm
from app.errors import AIError
from app.schemas import (
    QUESTION_OUTPUTS,
    CategoriesOut,
    FeedbackOut,
    FeedbackRequest,
    GenerateQuestionsRequest,
    JudgeOut,
    JudgeShortRequest,
    OcrText,
    ProcessMaterialRequest,
    QuestionSpec,
)
from app.validate import InvalidOutput, check_questions, fix_categories, normalize

PROMPTS = Path(__file__).parent / "prompts"
OCR_PROMPT = (PROMPTS / "OCR.md").read_text(encoding="utf-8")
AI01_PROMPT = (PROMPTS / "AI-01.md").read_text(encoding="utf-8")
AI02_PROMPT = (PROMPTS / "AI-02.md").read_text(encoding="utf-8")
AI03_PROMPT = (PROMPTS / "AI-03.md").read_text(encoding="utf-8")
AI06_PROMPT = (PROMPTS / "AI-06.md").read_text(encoding="utf-8")

MAX_FILE_BYTES = 30 * 1024 * 1024
DOWNLOAD_SECONDS = 30
MIN_TOTAL_CHARS = 200

AI01_SECONDS = 270  # API 명세의 서버 시간 상한
AI01_ATTEMPTS = 2
AI01_MIN_RETRY_SECONDS = 60  # 남은 시간이 이보다 적으면 단원화를 다시 하지 않는다
AI01_MAX_OUTPUT = 16000

# 추가 생성이 60초 이내여야 한다(기능명세서 5장). 요청만으로 사전 생성과 구분할 수 없어 둘 다 55초다.
# 다 못 만든 몫은 shortfall로 돌려주고, 백엔드가 모자란 몫만 다시 요청한다.
AI02_SECONDS = 55
AI02_ROUNDS = 3  # 첫 생성 + 부족분 재생성 2번
AI02_MIN_ROUND_SECONDS = 20  # 남은 시간이 이보다 적으면 재생성하지 않는다
AI02_EXTRA = 1.2  # 검증에서 버려질 몫을 생각해 더 요청한다
AI02_CHUNK = 10  # 한 호출에서 만드는 최대 문제 수. 출력이 길수록 느려져 나눠서 동시에 부른다
AI02_TOKENS_PER_QUESTION = 600

AI03_SECONDS = 4  # 사용자가 화면에서 기다린다. 실패하면 앱이 자가 판정으로 넘어가서 재시도하지 않는다
AI03_MAX_ANSWER = 200
AI06_SECONDS = 13
AI06_MAX_WRONG_ITEMS = 20
AI06_MAX_STEM = 100
AI06_TOP_CONCEPTS = 3

OCR_SECONDS = 120  # 단원화에 쓸 시간을 남기기 위한 OCR 몫
OCR_CONCURRENCY = 8
OCR_ATTEMPTS = 2
OCR_MAX_OUTPUT = 4000


# ---------- AI-01 단원화·요약 ----------


async def process_material(req: ProcessMaterialRequest) -> dict:
    deadline = llm.Deadline(AI01_SECONDS)
    files = await download([str(u) for u in req.file_urls], deadline)
    return await process_files(files, req.file_type, deadline)


async def process_files(files: list[bytes], file_type: str, deadline: llm.Deadline) -> dict:
    """다운로드 다음 단계. eval 스크립트가 로컬 파일로 직접 부른다."""
    usage = llm.Usage(model=llm.MODELS["AI01"])
    pages = await asyncio.to_thread(extract.extract, files, file_type)
    await _ocr(pages, files, file_type, deadline, usage)
    if sum(len(p.text) for p in pages) < MIN_TOTAL_CHARS:
        raise AIError("TEXT_EXTRACTION_FAILED")

    categories = await _categorize(pages, deadline, usage)
    return {
        "pageCount": len(pages),
        "pages": [{"page": p.page, "text": p.text} for p in pages],
        "categories": categories,
        "meta": usage.meta(),
    }


async def download(urls: list[str], deadline: llm.Deadline, client: httpx2.AsyncClient | None = None) -> list[bytes]:
    host = os.environ.get("SUPABASE_HOST")
    if not host or any(urlparse(u).hostname != host for u in urls):
        raise AIError("INVALID_REQUEST", "허용하지 않은 파일 주소예요.")

    own = client is None
    # 리다이렉트를 따라가지 않는다. 허용한 호스트 밖으로 나가지 않게 하기 위해서다.
    client = client or httpx2.AsyncClient(timeout=min(DOWNLOAD_SECONDS, deadline.remaining()), follow_redirects=False)
    try:
        return list(await asyncio.gather(*(_fetch(client, u) for u in urls)))
    finally:
        if own:
            await client.aclose()


async def _fetch(client: httpx2.AsyncClient, url: str) -> bytes:
    try:
        async with client.stream("GET", url) as r:
            if r.status_code != 200:
                raise AIError("FILE_FETCH_FAILED")
            data = bytearray()
            async for chunk in r.aiter_bytes():
                data += chunk
                if len(data) > MAX_FILE_BYTES:
                    raise AIError("UNSUPPORTED_FILE", "파일이 30MB를 넘어요.")
            return bytes(data)
    except httpx2.HTTPError:
        raise AIError("FILE_FETCH_FAILED") from None


async def _ocr(pages, files, file_type, deadline, usage):
    targets = [p for p in pages if p.needs_ocr]
    if not targets or file_type == "PPT":  # 슬라이드 속 이미지 글자는 MVP에서 읽지 않는다
        return
    if file_type == "PDF":
        images = await asyncio.to_thread(extract.render_pdf_pages, files[0], [p.page for p in targets])
    else:
        images = {p.page: await asyncio.to_thread(extract.to_jpeg, files[p.page - 1]) for p in targets}

    ocr_deadline = llm.Deadline(min(OCR_SECONDS, deadline.remaining()))
    gate = asyncio.Semaphore(OCR_CONCURRENCY)

    async def read(page):
        image = {"type": "input_image", "image_url": _data_url(images[page.page]), "detail": "high"}
        page.text = ""  # 끝내 못 읽으면 빈 쪽으로 둔다. 깨진 글자를 남기지 않는다
        async with gate:
            for _ in range(OCR_ATTEMPTS):
                try:
                    out = await llm.parse(
                        "OCR",
                        instructions=OCR_PROMPT,
                        input=[{"role": "user", "content": [image]}],
                        schema=OcrText,
                        deadline=ocr_deadline,
                        usage=usage,
                        max_output_tokens=OCR_MAX_OUTPUT,
                        count_attempt=False,
                    )
                    page.text = extract.clean(out.text)
                    return
                except AIError as e:
                    if e.code == "LLM_TIMEOUT":
                        return

    await asyncio.gather(*(read(p) for p in targets))


def _data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


async def _categorize(pages, deadline, usage) -> list[dict]:
    document = "\n".join(f'<page n="{p.page}">\n{p.text}\n</page>' for p in pages)
    base_input = f"총 {len(pages)}쪽\n<document>\n{document}\n</document>"
    lengths = [len(p.text) for p in pages]
    note, error = "", AIError("LLM_FAILED")

    for _ in range(AI01_ATTEMPTS):
        try:
            out = await llm.parse(
                "AI01",
                instructions=AI01_PROMPT,
                input=base_input + note,
                schema=CategoriesOut,
                deadline=deadline,
                usage=usage,
                max_output_tokens=AI01_MAX_OUTPUT,
            )
            return fix_categories(out.categories, lengths)
        except InvalidOutput as e:
            note = f"\n\n이전 출력에 문제가 있었다: {e}. 규칙에 맞게 다시 나눠라."
            error = AIError("LLM_FAILED")
        except AIError as e:
            error = e
        if deadline.remaining() < AI01_MIN_RETRY_SECONDS:
            break
    raise error


# ---------- AI-02 문제·해설 생성 ----------

DIFFICULTY_NAMES = {1: "하", 2: "중", 3: "상"}


async def generate_questions(req: GenerateQuestionsRequest, rng: random.Random | None = None) -> dict:
    deadline = llm.Deadline(AI02_SECONDS)
    usage = llm.Usage(model=llm.MODELS["AI02"])
    pages = {p.page: p.text for p in req.category.pages}
    document = "\n".join(f'<page n="{n}">\n{t}\n</page>' for n, t in pages.items())
    rejected = {"schema": 0, "evidence": 0, "duplicate": 0}
    rng = rng or random.Random()

    results = await asyncio.gather(
        *(_generate_spec(s, req, pages, document, deadline, usage, rejected, rng) for s in req.requests)
    )
    questions = [q for qs in results for q in qs]
    if not questions:
        raise AIError("LLM_FAILED")
    shortfall = [
        {"type": s.type, "difficulty": s.difficulty, "count": s.count - len(qs)}
        for s, qs in zip(req.requests, results)
        if len(qs) < s.count
    ]
    return {"questions": questions, "shortfall": shortfall, "rejected": rejected, "meta": usage.meta()}


async def _generate_spec(spec: QuestionSpec, req, pages, document, deadline, usage, rejected, rng) -> list[dict]:
    """유형·난이도 하나의 문제를 만든다. 검증에서 빠진 만큼만 다시 만든다."""
    accepted: list[dict] = []
    for _ in range(AI02_ROUNDS):
        need = spec.count - len(accepted)
        if need <= 0 or deadline.remaining() < AI02_MIN_ROUND_SECONDS:
            break
        exclude = req.exclude_stems + [q["stem"] for q in accepted]
        ask = math.ceil(need * AI02_EXTRA)
        chunks = [min(AI02_CHUNK, ask - i) for i in range(0, ask, AI02_CHUNK)]
        outs = await asyncio.gather(
            *(_ask_questions(spec, n, req.category.category_id, document, exclude, deadline, usage) for n in chunks),
            return_exceptions=True,
        )
        drafts = [d for out in outs if not isinstance(out, BaseException) for d in out.questions]
        for out in outs:
            if isinstance(out, BaseException) and not isinstance(out, AIError):
                raise out
        new, counts = check_questions(drafts, spec.type, spec.difficulty, pages, exclude, rng)
        for reason, n in counts.items():
            rejected[reason] += n
        accepted += new
    return accepted[: spec.count]


async def _ask_questions(spec: QuestionSpec, count: int, category_id: int, document: str, exclude, deadline, usage):
    # 자료를 앞에 두어 같은 단원의 호출끼리 프롬프트 캐시를 나눠 쓴다.
    existing = "\n".join(f"- {s}" for s in exclude[-60:]) or "(없음)"
    request = (
        f"유형: {spec.type}\n난이도: {spec.difficulty} ({DIFFICULTY_NAMES[spec.difficulty]})\n개수: {count}\n"
        f"이미 있는 문제:\n{existing}"
    )
    return await llm.parse(
        "AI02",
        instructions=AI02_PROMPT,
        input=f"<document>\n{document}\n</document>\n\n{request}",
        schema=QUESTION_OUTPUTS[spec.type],
        deadline=deadline,
        usage=usage,
        max_output_tokens=count * AI02_TOKENS_PER_QUESTION + 2000,
        cache_key=f"category-{category_id}",
    )


# ---------- AI-03 단답형 판정 ----------


async def judge_short(req: JudgeShortRequest) -> dict:
    answer = req.user_answer.strip()
    if not answer:
        return {"correct": False, "reason": "답을 입력하지 않았어요."}
    # 백엔드도 먼저 비교하지만, 정규화 기준이 달라도 AI를 부르지 않게 여기서 한 번 더 본다.
    if normalize(answer) in {normalize(a) for a in req.accepted_answers}:
        return {"correct": True, "reason": "정답과 같아요."}

    accepted = ", ".join(req.accepted_answers)
    out = await llm.parse(
        "AI03",
        instructions=AI03_PROMPT,
        input=(
            f"문제: {req.stem}\n정답 목록: {accepted}\n해설: {req.explanation}\n"
            f"<user_answer>{answer[:AI03_MAX_ANSWER]}</user_answer>"
        ),
        schema=JudgeOut,
        deadline=llm.Deadline(AI03_SECONDS),
        usage=llm.Usage(model=llm.MODELS["AI03"]),
        max_output_tokens=300,
    )
    return {"correct": out.correct, "reason": out.reason.strip()}


# ---------- AI-06 통학 후 피드백 ----------


async def session_feedback(req: FeedbackRequest) -> dict:
    if req.answered == 0:
        raise AIError("INVALID_REQUEST", "푼 문제가 없는 세션은 피드백을 만들지 않아요.")

    # 개념 집계는 코드가 한다. LLM에는 집계 결과만 넘겨 없는 개념을 지어내지 않게 한다.
    wrong = _top(tag for item in req.wrong_items for tag in item.concept_tags)
    good = _top(c for c in req.correct_concepts if c not in wrong)
    stems = "\n".join(f"- {item.stem[:AI06_MAX_STEM]}" for item in req.wrong_items[:AI06_MAX_WRONG_ITEMS])
    summary = (
        f"모드: {'복습' if req.mode == 'REVIEW' else '커리큘럼'}\n"
        f"문제 {req.total}개 중 {req.answered}개를 풀고 {req.correct}개를 맞혔다.\n"
        f"잘한 개념: {', '.join(good) or '(없음)'}\n"
        f"헷갈린 개념: {', '.join(wrong) or '(없음)'}\n"
        f"틀린 문제:\n{stems or '(없음)'}"
    )
    out = await llm.parse(
        "AI06",
        instructions=AI06_PROMPT,
        input=summary,
        schema=FeedbackOut,
        deadline=llm.Deadline(AI06_SECONDS),
        usage=llm.Usage(model=llm.MODELS["AI06"]),
        max_output_tokens=400,
    )
    return {"feedback": out.feedback.strip()}


def _top(items) -> list[str]:
    """많이 나온 순서로 상위 몇 개. 같은 횟수면 먼저 나온 것이 앞이다."""
    return [name for name, _ in Counter(i.strip() for i in items if i.strip()).most_common(AI06_TOP_CONCEPTS)]

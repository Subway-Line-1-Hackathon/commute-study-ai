"""기능별 처리 흐름. 입출력은 docs/API_명세.md, 단계는 docs/기능별_개발계획.md를 따른다."""

import asyncio
import base64
import logging
from collections import Counter
from collections.abc import AsyncIterator
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
    IssuesOut,
    OcrText,
    PlanOut,
    Point,
    ProcessMaterialRequest,
    QuestionSpec,
    TutorAskRequest,
)
from app.validate import InvalidOutput, assign_points, attach_notes, check_issues, capacity, check_points, check_questions, fix_categories, normalize

log = logging.getLogger("pipelines")
PROMPTS = Path(__file__).parent / "prompts"
OCR_PROMPT = (PROMPTS / "OCR.md").read_text(encoding="utf-8")
OCR_NOTE_PROMPT = (PROMPTS / "OCR-NOTE.md").read_text(encoding="utf-8")
AI01_PROMPT = (PROMPTS / "AI-01.md").read_text(encoding="utf-8")
AI01_CHECK_PROMPT = (PROMPTS / "AI-01-CHECK.md").read_text(encoding="utf-8")
AI02_PROMPT = (PROMPTS / "AI-02.md").read_text(encoding="utf-8")
AI02_PLAN_PROMPT = (PROMPTS / "AI-02-PLAN.md").read_text(encoding="utf-8")
AI03_PROMPT = (PROMPTS / "AI-03.md").read_text(encoding="utf-8")
AI05_PROMPT = (PROMPTS / "AI-05.md").read_text(encoding="utf-8")
AI06_PROMPT = (PROMPTS / "AI-06.md").read_text(encoding="utf-8")

MAX_FILE_BYTES = 30 * 1024 * 1024
DOWNLOAD_SECONDS = 30
MIN_TOTAL_CHARS = 200

AI01_SECONDS = 270  # API 명세의 서버 시간 상한
AI01_ATTEMPTS = 2
AI01_MIN_RETRY_SECONDS = 60  # 남은 시간이 이보다 적으면 단원화를 다시 하지 않는다
AI01_MAX_OUTPUT = 16000
AI01_CHECK_MAX_OUTPUT = 4000

# 추가 생성이 60초 이내여야 한다(기능명세서 5장). 요청만으로 사전 생성과 구분할 수 없어 둘 다 55초다.
# 출제 계획(포인트 추출) → 코드 배정 → 포인트당 문제 하나 (docs/AI-02_출제_재설계.md).
# 자료가 받쳐 주는 만큼만 만든다. 그 상한이 capacity이고, 요청보다 적으면 shortfall이 생긴다.
AI02_SECONDS = 55
AI02_PLAN_MAX_OUTPUT = 12000
AI02_ROUNDS = 2  # 첫 생성 + 탈락한 포인트만 한 번 더
AI02_MIN_ROUND_SECONDS = 10  # 남은 시간이 이보다 적으면 다시 만들지 않는다. 시간 안에 못 끝내도 만든 문제는 남는다
AI02_CHUNK = 10  # 한 호출에서 만드는 최대 문제 수. 출력이 길수록 느려져 나눠서 동시에 부른다
AI02_TOKENS_PER_QUESTION = 600

AI03_SECONDS = 4  # 사용자가 화면에서 기다린다. 실패하면 앱이 자가 판정으로 넘어가서 재시도하지 않는다
AI03_MAX_ANSWER = 200
AI05_SECONDS = 13  # 앱의 전체 10초 목표보다 길고 백엔드 타임아웃 15초보다 짧다
AI05_MAX_TURN = 1000  # 이전 대화와 이번 질문을 이 글자 수에서 자른다
AI05_MAX_OUTPUT = 600
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
    urls = [str(u) for u in req.file_urls]
    files = await download(urls + [str(u) for u in req.note_urls], deadline)
    return await process_files(files[: len(urls)], req.file_type, deadline, notes=files[len(urls) :])


async def process_files(files: list[bytes], file_type: str, deadline: llm.Deadline, notes: list[bytes] = ()) -> dict:
    """다운로드 다음 단계. eval 스크립트가 로컬 파일로 직접 부른다. notes는 필기 사진이다."""
    usage = llm.Usage(model=llm.MODELS["AI01"])
    pages = await asyncio.to_thread(extract.extract, files, file_type)
    note_pages = await asyncio.to_thread(extract.extract, list(notes), "JPG") if notes else []
    await asyncio.gather(
        _ocr(pages, files, file_type, deadline, usage),
        _ocr(note_pages, list(notes), "JPG", deadline, usage, feature="OCR_NOTE", prompt=OCR_NOTE_PROMPT),
    )
    for p in note_pages:  # 필기는 강의자료 다음 쪽 번호를 쓴다. 문제 근거·AI 질문이 쪽 번호 하나로 둘 다 가리킨다
        p.page += len(pages)
    if sum(len(p.text) for p in pages + note_pages) < MIN_TOTAL_CHARS:
        raise AIError("TEXT_EXTRACTION_FAILED")

    document = _document(pages, note_pages)
    categories, issues = await asyncio.gather(
        _categorize(pages, note_pages, document, deadline, usage), _check_concepts(pages + note_pages, document, deadline, usage)
    )
    return {
        "pageCount": len(pages) + len(note_pages),
        "pages": [{"page": p.page, "text": p.text} for p in pages + note_pages],
        "notePages": [p.page for p in note_pages],
        "categories": categories,
        "issues": issues,
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


async def _ocr(pages, files, file_type, deadline, usage, feature="OCR", prompt=OCR_PROMPT):
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
                        feature,
                        instructions=prompt,
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


def _document(pages, note_pages) -> str:
    lecture = "\n".join(f'<page n="{p.page}">\n{p.text}\n</page>' for p in pages)
    notes = "\n".join(f'<note n="{p.page}" label="필기 {p.page - len(pages)}쪽">\n{p.text}\n</note>' for p in note_pages)
    return f"강의자료 총 {len(pages)}쪽, 필기 {len(note_pages)}쪽\n<document>\n{lecture}\n{notes}\n</document>"


async def _check_concepts(all_pages, document, deadline, usage) -> list[dict]:
    """자료·필기에서 명백히 틀린 내용을 찾는다 (11-07 팀 결정). 점검은 보조 기능이라 실패해도 단원화를 막지 않는다."""
    try:
        out = await llm.parse(
            "AI01_CHECK",
            instructions=AI01_CHECK_PROMPT,
            input=document,
            schema=IssuesOut,
            deadline=deadline,
            usage=usage,
            max_output_tokens=AI01_CHECK_MAX_OUTPUT,
            count_attempt=False,
        )
    except AIError as e:
        log.warning("AI01_CHECK 실패, issues 없이 진행: %s", e.code)
        return []
    issues = check_issues(out.issues, {p.page: p.text for p in all_pages})
    log.info("AI01_CHECK issues=%d (모델 %d개 중 원문 확인)", len(issues), len(out.issues))
    return issues


async def _categorize(pages, note_pages, document, deadline, usage) -> list[dict]:
    base_input = document
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
            cats = fix_categories(out.categories, lengths)
            return attach_notes(cats, out.notes, {p.page: p.text for p in pages}, {p.page: p.text for p in note_pages})
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

    points = await _plan(req, pages, document, deadline, usage)
    # capacity는 요청 유형과 상관없는 단원 전체 수다. 백엔드가 유형별로 따로 요청해도 같은 값을 받는다.
    limit = capacity(points, dict.fromkeys(["OX", "MC", "SHORT", *(s.type for s in req.requests)]))
    # 이미 있는 문제는 유형을 모르므로 단원 전체 수에서만 뺀다. 같은 문제는 중복 검사로 거른다.
    # (요청 유형의 capacity에서 빼면, 단답만 추가로 요청할 때 O/X·객관식 수까지 빠져 0개가 됐다. #49)
    budget = min(sum(s.count for s in req.requests), limit - len(req.exclude_stems))
    assigned = assign_points(points, req.requests, budget)
    results = await asyncio.gather(
        *(
            _generate_spec(s, nums, points, req, pages, document, deadline, usage, rejected, rng)
            for s, nums in zip(req.requests, assigned)
        )
    )
    questions = [q for qs in results for q in qs]
    if any(assigned) and not questions:
        raise AIError("LLM_FAILED")
    shortfall = [
        {"type": s.type, "difficulty": s.difficulty, "count": s.count - len(qs)}
        for s, qs in zip(req.requests, results)
        if len(qs) < s.count
    ]
    log.info(
        "AI02 category=%s points=%d capacity=%d existing=%d %s rejected=%s %dms",
        req.category.category_id,
        len(points),
        limit,
        len(req.exclude_stems),
        " ".join(
            f"{s.type}/{s.difficulty}:요청{s.count}·배정{len(nums)}·생성{len(qs)}"
            for s, nums, qs in zip(req.requests, assigned, results)
        ),
        rejected,
        usage.meta()["elapsedMs"],
    )
    return {"questions": questions, "shortfall": shortfall, "capacity": limit, "rejected": rejected, "meta": usage.meta()}


async def _plan(req: GenerateQuestionsRequest, pages, document, deadline, usage) -> list[Point]:
    """단원에서 물을 가치가 있는 출제 포인트를 뽑는다. 근거가 자료에 없는 포인트는 버린다."""
    out = await llm.parse(
        "AI02",
        instructions=AI02_PLAN_PROMPT,
        input=f"단원: {req.category.name}\n<document>\n{document}\n</document>",
        schema=PlanOut,
        deadline=deadline,
        usage=usage,
        max_output_tokens=AI02_PLAN_MAX_OUTPUT,
        cache_key=f"category-{req.category.category_id}",
    )
    points = check_points(out.points, pages)
    if not points:
        raise AIError("LLM_FAILED")
    return points


async def _generate_spec(spec: QuestionSpec, nums: list[int], points, req, pages, document, deadline, usage, rejected, rng):
    """배정된 포인트마다 문제 하나를 만든다. 검증에서 빠진 포인트만 다시 만든다."""
    accepted: list[dict] = []
    todo = nums
    for round_ in range(AI02_ROUNDS):
        if not todo or (round_ and deadline.remaining() < AI02_MIN_ROUND_SECONDS):
            break
        exclude = req.exclude_stems + [q["stem"] for q in accepted]
        chunks = [todo[i : i + AI02_CHUNK] for i in range(0, len(todo), AI02_CHUNK)]
        outs = await asyncio.gather(
            *(_ask_questions(spec, chunk, points, req.category.category_id, document, exclude, deadline, usage) for chunk in chunks),
            return_exceptions=True,
        )
        for out in outs:
            if isinstance(out, BaseException) and not isinstance(out, AIError):
                raise out
        drafts = [d for out in outs if not isinstance(out, BaseException) for d in out.questions]
        mine = {n: points[n - 1] for n in todo}
        new, counts = check_questions(drafts, spec.type, spec.difficulty, mine, pages, exclude, rng)
        for reason, n in counts.items():
            rejected[reason] += n
        accepted += new
        done = {q["point"] for q in new}
        todo = [n for n in todo if n not in done]
    for q in accepted:
        del q["point"]
    return accepted


async def _ask_questions(spec: QuestionSpec, nums: list[int], points, category_id: int, document: str, exclude, deadline, usage):
    # 자료를 앞에 두어 같은 단원의 호출끼리 프롬프트 캐시를 나눠 쓴다.
    existing = "\n".join(f"- {s}" for s in exclude[-60:]) or "(없음)"
    listed = "\n".join(
        f'{n}. {points[n - 1].statement}\n   근거({points[n - 1].source_page}쪽): "{points[n - 1].evidence_quote}"' for n in nums
    )
    request = (
        f"유형: {spec.type}\n난이도: {spec.difficulty} ({DIFFICULTY_NAMES[spec.difficulty]})\n개수: {len(nums)}\n"
        f"출제 포인트:\n{listed}\n\n이미 있는 문제:\n{existing}"
    )
    return await llm.parse(
        "AI02",
        instructions=AI02_PROMPT,
        input=f"<document>\n{document}\n</document>\n\n{request}",
        schema=QUESTION_OUTPUTS[spec.type],
        deadline=deadline,
        usage=usage,
        max_output_tokens=len(nums) * AI02_TOKENS_PER_QUESTION + 2000,
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


# ---------- AI-05 AI 질문 ----------


async def tutor_ask(req: TutorAskRequest, usage: llm.Usage) -> AsyncIterator[str]:
    """답변 조각을 차례로 내준다. 실패는 AIError로 끝난다."""
    q = req.question
    document = "\n".join(f'<page n="{p.page}">\n{p.text}\n</page>' for p in req.category.pages)
    choices = "".join(f"\n{i}. {c}" for i, c in enumerate(q.choices, 1))
    question = (
        f"<question>\n문제: {q.stem}{choices}\n정답: {q.answer}\n해설: {q.explanation}\n"
        f"학생 답: {q.user_answer or '(답하지 않음)'}\n</question>"
    )
    # 단원 자료를 맨 앞에 둬서 같은 단원의 질문끼리 프롬프트 캐시를 나눠 쓴다.
    messages = [{"role": "user", "content": f"단원: {req.category.name}\n<document>\n{document}\n</document>\n\n{question}"}]
    messages += [{"role": t.role, "content": t.content[:AI05_MAX_TURN]} for t in req.history]
    messages.append({"role": "user", "content": req.message[:AI05_MAX_TURN]})

    async for delta in llm.stream(
        "AI05",
        instructions=AI05_PROMPT,
        input=messages,
        deadline=llm.Deadline(AI05_SECONDS),
        usage=usage,
        max_output_tokens=AI05_MAX_OUTPUT,
        cache_key=f"category-{req.category.category_id}",
    ):
        yield delta


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

"""기능별 처리 흐름. 입출력은 docs/API_명세.md, 단계는 docs/기능별_개발계획.md를 따른다."""

import asyncio
import base64
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx2

from app import extract, llm
from app.errors import AIError
from app.schemas import CategoriesOut, OcrText, ProcessMaterialRequest
from app.validate import InvalidOutput, fix_categories

PROMPTS = Path(__file__).parent / "prompts"
OCR_PROMPT = (PROMPTS / "OCR.md").read_text(encoding="utf-8")
AI01_PROMPT = (PROMPTS / "AI-01.md").read_text(encoding="utf-8")

MAX_FILE_BYTES = 30 * 1024 * 1024
DOWNLOAD_SECONDS = 30
MIN_TOTAL_CHARS = 200

AI01_SECONDS = 270  # API 명세의 서버 시간 상한
AI01_ATTEMPTS = 2
AI01_MIN_RETRY_SECONDS = 60  # 남은 시간이 이보다 적으면 단원화를 다시 하지 않는다
AI01_MAX_OUTPUT = 16000

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

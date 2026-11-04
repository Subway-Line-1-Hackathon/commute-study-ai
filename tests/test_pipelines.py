import asyncio
import io
import os

os.environ.setdefault("AI_INTERNAL_KEY", "test-key")

import httpx2
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_extract import make_pdf

from app import extract, llm, main, pipelines
from app.errors import AIError
from app.schemas import CategoriesOut, CategoryDraft, OcrText, ProcessMaterialRequest

LINE = "Stack is a LIFO structure and queue is a FIFO structure used in many algorithms"  # 80자
HOST = "x.supabase.co"


def request(file_type="PDF", urls=1) -> ProcessMaterialRequest:
    return ProcessMaterialRequest(
        material_id=1,
        file_type=file_type,
        kind="LECTURE",
        file_name="a",
        file_urls=[f"https://{HOST}/f{i}" for i in range(urls)],
    )


def categories(*starts) -> CategoriesOut:
    return CategoriesOut(
        categories=[CategoryDraft(name=f"단원{s}", page_start=s, page_end=s, summary="- 요약 (1쪽)") for s in starts]
    )


@pytest.fixture
def fake(monkeypatch):
    """llm.parse를 기능별 응답 목록으로 바꾼다. 목록의 예외는 그대로 던진다."""

    def install(files: list[bytes], **answers):
        calls = []

        async def parse(feature, *, usage, count_attempt=True, **kwargs):
            calls.append((feature, kwargs))
            if count_attempt:
                usage.attempts += 1
            usage.input_tokens += 10
            queue = answers[feature]
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item

        async def download(urls, deadline):
            return files

        monkeypatch.setattr(llm, "parse", parse)
        monkeypatch.setattr(pipelines, "download", download)
        return calls

    return install


def run(req=None):
    return asyncio.run(pipelines.process_material(req or request()))


def error_code(req=None) -> str:
    with pytest.raises(AIError) as e:
        run(req)
    return e.value.code


def test_text_pdf_is_split_into_categories(fake):
    calls = fake([make_pdf([LINE] * 10)], AI01=[categories(1, 6)])
    out = run()

    assert out["pageCount"] == 10
    assert out["pages"][0] == {"page": 1, "text": LINE}
    assert [(c["order"], c["pageStart"], c["pageEnd"]) for c in out["categories"]] == [(1, 1, 5), (2, 6, 10)]
    assert out["meta"]["attempts"] == 1
    assert [f for f, _ in calls] == ["AI01"]
    assert '<page n="10">' in calls[0][1]["input"]


def test_invalid_output_is_retried_with_reason(fake):
    calls = fake([make_pdf([LINE] * 10)], AI01=[categories(1, 1), categories(1, 6)])
    out = run()
    assert out["meta"]["attempts"] == 2
    assert "이전 출력에 문제가 있었다" in calls[1][1]["input"]


def test_two_failures_end_in_llm_failed(fake):
    fake([make_pdf([LINE] * 10)], AI01=[categories(1, 1)])
    assert error_code() == "LLM_FAILED"


def test_llm_error_is_retried_once(fake):
    fake([make_pdf([LINE] * 10)], AI01=[AIError("LLM_FAILED"), categories(1)])
    assert run()["meta"]["attempts"] == 2


def test_scanned_pages_are_read_by_ocr(fake):
    calls = fake([make_pdf([LINE] * 5 + ["", ""])], OCR=[OcrText(text="  스캔한 쪽의 글자  ")], AI01=[categories(1)])
    out = run()
    assert out["pages"][5]["text"] == "스캔한 쪽의 글자"
    assert [f for f, _ in calls].count("OCR") == 2
    assert out["meta"]["attempts"] == 1  # OCR은 시도 횟수에 세지 않는다
    image = calls[0][1]["input"][0]["content"][0]
    assert image["type"] == "input_image"
    assert image["image_url"].startswith("data:image/jpeg;base64,")


def test_ocr_failure_leaves_page_empty(fake):
    calls = fake([make_pdf([LINE] * 5 + [""])], OCR=[AIError("LLM_FAILED")], AI01=[categories(1)])
    out = run()
    assert out["pages"][5]["text"] == ""
    assert [f for f, _ in calls].count("OCR") == 2  # 한 번 더 시도한 뒤 포기


def test_too_little_text_fails(fake):
    fake([make_pdf(["", ""])], OCR=[OcrText(text="")], AI01=[categories(1)])
    assert error_code() == "TEXT_EXTRACTION_FAILED"


def png(size=(3000, 1000)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()


def test_png_pages_go_through_ocr(fake):
    calls = fake([png(), png()], OCR=[OcrText(text=LINE * 2)], AI01=[categories(1)])
    out = run(request("PNG", urls=2))
    assert [p["text"] for p in out["pages"]] == [LINE * 2] * 2
    assert [f for f, _ in calls] == ["OCR", "OCR", "AI01"]


def test_images_are_shrunk_to_jpeg():
    jpeg = extract.to_jpeg(png())
    assert jpeg[:2] == b"\xff\xd8"
    assert max(Image.open(io.BytesIO(jpeg)).size) == extract.IMAGE_MAX_SIDE
    rendered = extract.render_pdf_pages(make_pdf([LINE, ""]), [2])
    assert max(Image.open(io.BytesIO(rendered[2])).size) == extract.IMAGE_MAX_SIDE


# ---------- 다운로드 ----------


def fetch(urls, handler, monkeypatch, host=HOST):
    monkeypatch.setenv("SUPABASE_HOST", host)
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return asyncio.run(pipelines.download(urls, llm.Deadline(10), client))


def fetch_error(urls, handler, monkeypatch, host=HOST) -> str:
    with pytest.raises(AIError) as e:
        fetch(urls, handler, monkeypatch, host)
    return e.value.code


def test_download_returns_bytes_in_order(monkeypatch):
    files = fetch([f"https://{HOST}/a", f"https://{HOST}/b"], lambda r: httpx2.Response(200, content=r.url.path.encode()), monkeypatch)
    assert files == [b"/a", b"/b"]


def test_download_rejects_other_hosts(monkeypatch):
    assert fetch_error(["https://evil.example.com/a"], lambda r: httpx2.Response(200), monkeypatch) == "INVALID_REQUEST"
    assert fetch_error([f"https://{HOST}/a"], lambda r: httpx2.Response(200), monkeypatch, host="") == "INVALID_REQUEST"


def test_download_errors(monkeypatch):
    assert fetch_error([f"https://{HOST}/a"], lambda r: httpx2.Response(404), monkeypatch) == "FILE_FETCH_FAILED"
    assert fetch_error([f"https://{HOST}/a"], lambda r: httpx2.Response(302), monkeypatch) == "FILE_FETCH_FAILED"

    def broken(r):
        raise httpx2.ConnectError("down")

    assert fetch_error([f"https://{HOST}/a"], broken, monkeypatch) == "FILE_FETCH_FAILED"


def test_download_stops_over_size_limit(monkeypatch):
    monkeypatch.setattr(pipelines, "MAX_FILE_BYTES", 10)
    assert fetch_error([f"https://{HOST}/a"], lambda r: httpx2.Response(200, content=b"x" * 11), monkeypatch) == "UNSUPPORTED_FILE"


# ---------- API 연결 ----------

BODY = {"materialId": 1, "fileType": "PDF", "kind": "LECTURE", "fileName": "a", "fileUrls": [f"https://{HOST}/a"]}
KEY = {"X-Internal-Key": os.environ["AI_INTERNAL_KEY"]}


def test_route_uses_pipeline_when_mock_is_off(monkeypatch):
    async def real(req):
        return {"real": True}

    monkeypatch.setattr(main, "MOCK", False)
    monkeypatch.setattr(pipelines, "process_material", real)
    client = TestClient(main.app)
    assert client.post("/v1/materials/process", json=BODY, headers=KEY).json() == {"real": True}
    # 실제 모드에서는 X-Mock-Error를 무시한다
    r = client.post("/v1/materials/process", json=BODY, headers={**KEY, "X-Mock-Error": "LLM_FAILED"})
    assert r.json() == {"real": True}


def test_pipeline_errors_use_api_error_format(monkeypatch):
    async def failing(req):
        raise AIError("TEXT_EXTRACTION_FAILED")

    monkeypatch.setattr(main, "MOCK", False)
    monkeypatch.setattr(pipelines, "process_material", failing)
    r = TestClient(main.app).post("/v1/materials/process", json=BODY, headers=KEY)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "TEXT_EXTRACTION_FAILED"

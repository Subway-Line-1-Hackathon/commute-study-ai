"""업로드 파일 → 쪽별 텍스트.

OCR이 필요한 쪽은 needs_ocr로 표시만 한다. 실제로 읽는 일은 LLM 단계가 맡는다.
"""

import io
import re
import threading
import unicodedata
import zipfile
from dataclasses import dataclass

import pypdfium2 as pdfium
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.errors import AIError

MAX_PAGES = {"PDF": 100, "PPT": 100, "PNG": 20}
MIN_CHARS = 30  # 이보다 적으면 스캔 쪽으로 본다
MIN_VALID_RATIO = 0.5  # 정상 문자가 이보다 적으면 깨진 글꼴로 본다

# pdfium은 여러 스레드에서 동시에 호출할 수 없다.
# ponytail: 전역 잠금이라 PDF 처리가 한 번에 하나씩 돈다. 느려지면 프로세스 풀로 바꾼다.
_pdfium_lock = threading.Lock()


@dataclass
class ExtractedPage:
    page: int
    text: str
    needs_ocr: bool


def extract(files: list[bytes], file_type: str) -> list[ExtractedPage]:
    """files는 PDF·PPT면 1개, PNG면 쪽 순서대로 여러 개다."""
    if file_type == "PNG":
        _check_limit(len(files), "PNG")
        for data in files:
            _expect_type(data, "PNG")
        return [ExtractedPage(i, "", True) for i in range(1, len(files) + 1)]

    data = files[0]
    _expect_type(data, file_type)
    texts = _pdf_texts(data) if file_type == "PDF" else _pptx_texts(data)
    pages = []
    for i, raw in enumerate(texts, 1):
        text = clean(raw)
        pages.append(ExtractedPage(i, text, needs_ocr(text)))
    return pages


def detect_type(data: bytes) -> str:
    if b"%PDF" in data[:1024]:
        return "PDF"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if any(name.startswith("ppt/") for name in z.namelist()):
                    return "PPT"
        except zipfile.BadZipFile:
            pass
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        raise AIError("UNSUPPORTED_FILE", "예전 형식(.ppt) 파일이에요. PowerPoint에서 .pptx로 저장해 다시 올려주세요.")
    raise AIError("UNSUPPORTED_FILE")


def _expect_type(data: bytes, file_type: str):
    if detect_type(data) != file_type:
        raise AIError("UNSUPPORTED_FILE")


def _check_limit(count: int, file_type: str):
    if count > MAX_PAGES[file_type]:
        raise AIError("PAGE_LIMIT_EXCEEDED")


def _pdf_texts(data: bytes) -> list[str]:
    with _pdfium_lock:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError:
            raise AIError("UNSUPPORTED_FILE", "열 수 없는 PDF예요. 암호가 걸려 있다면 풀고 다시 올려주세요.") from None
        try:
            _check_limit(len(pdf), "PDF")  # 본문을 읽기 전에 쪽수부터 본다
            return [pdf[i].get_textpage().get_text_bounded() for i in range(len(pdf))]
        finally:
            pdf.close()


def _pptx_texts(data: bytes) -> list[str]:
    try:
        prs = Presentation(io.BytesIO(data))
    except Exception:
        raise AIError("UNSUPPORTED_FILE") from None
    _check_limit(len(prs.slides), "PPT")
    texts = []
    for slide in prs.slides:
        parts = [_shape_text(s) for s in _reading_order(slide.shapes)]
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"[노트] {notes}")
        texts.append("\n".join(p for p in parts if p))
    return texts


def _reading_order(shapes) -> list:
    # 위→아래, 왼쪽→오른쪽
    return sorted(shapes, key=lambda s: (s.top or 0, s.left or 0))


def _shape_text(shape) -> str:
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        return "\n".join(t for t in map(_shape_text, _reading_order(shape.shapes)) if t)
    if shape.has_table:
        return "\n".join(" | ".join(cell.text for cell in row.cells) for row in shape.table.rows)
    if shape.has_text_frame:
        return shape.text_frame.text
    return ""


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f­﻿]")


def clean(text: str) -> str:
    # NFC를 안 하면 맥에서 만든 PDF의 한글이 자모로 쪼개져 근거 대조가 실패한다.
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = (re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _is_garbage(ch: str) -> bool:
    # 글꼴에 글자 대응표가 없는 PDF는 사용자 영역 문자나 라틴 확장 문자(À, Ì 등)로 깨져 나온다.
    if ch == "�" or "\u0080" <= ch <= "ɏ":
        return True
    return unicodedata.category(ch) in ("Co", "Cn", "Cs", "Cc")


def needs_ocr(text: str) -> bool:
    chars = [ch for ch in text if not ch.isspace()]
    if len(chars) < MIN_CHARS:
        return True
    valid = sum(1 for ch in chars if not _is_garbage(ch))
    return valid / len(chars) < MIN_VALID_RATIO

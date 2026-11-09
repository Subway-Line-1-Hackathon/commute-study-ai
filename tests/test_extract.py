import io
import zipfile

import pytest
from pptx import Presentation
from pptx.util import Inches

from app.errors import AIError
from app.extract import clean, extract, needs_ocr

LONG = "Stack is a LIFO structure and queue is a FIFO structure"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def make_pdf(texts: list[str]) -> bytes:
    """Helvetica로 쪽마다 한 줄을 쓴 최소 PDF. 텍스트에는 괄호를 쓰지 않는다."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in texts:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objs.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objs)} 0 R >>"
        )
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(texts)} >>"

    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def pptx_bytes(prs) -> bytes:
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def blank_slides(n: int) -> bytes:
    prs = Presentation()
    for _ in range(n):
        prs.slides.add_slide(prs.slide_layouts[6])
    return pptx_bytes(prs)


def error_code(fn) -> str:
    with pytest.raises(AIError) as e:
        fn()
    return e.value.code


def test_pdf_pages_keep_numbers():
    pages = extract([make_pdf([LONG, "", LONG + " page three"])], "PDF")
    assert [p.page for p in pages] == [1, 2, 3]
    assert pages[0].text == LONG
    assert [p.needs_ocr for p in pages] == [False, True, False]


def test_pdf_page_limit():
    assert extract([make_pdf([""] * 100)], "PDF")[-1].page == 100
    assert error_code(lambda: extract([make_pdf([""] * 101)], "PDF")) == "PAGE_LIMIT_EXCEEDED"


def test_broken_pdf():
    assert error_code(lambda: extract([b"%PDF-1.4 not really"], "PDF")) == "UNSUPPORTED_FILE"


def test_old_ppt_format_is_rejected_with_hint():
    with pytest.raises(AIError) as e:
        extract([b"\xd0\xcf\x11\xe0" + b"\x00" * 100], "PPT")
    assert e.value.code == "UNSUPPORTED_FILE"
    assert ".pptx" in e.value.message


def test_declared_type_must_match():
    assert error_code(lambda: extract([make_pdf([LONG])], "PPT")) == "UNSUPPORTED_FILE"


def test_zip_that_is_not_pptx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<x/>")
    assert error_code(lambda: extract([buf.getvalue()], "PPT")) == "UNSUPPORTED_FILE"


def test_pptx_reading_order_table_group_notes():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(5), Inches(4), Inches(1)).text_frame.text = "bottom"
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text_frame.text = "top"
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    for r, row in enumerate([["a", "b"], ["c", "d"]]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    group = slide.shapes.add_group_shape()
    group.shapes.add_textbox(Inches(1), Inches(3), Inches(4), Inches(1)).text_frame.text = "grouped"
    slide.notes_slide.notes_text_frame.text = "speaker note"
    prs.slides.add_slide(prs.slide_layouts[6])

    pages = extract([pptx_bytes(prs)], "PPT")
    assert pages[0].text == "top\na | b\nc | d\ngrouped\nbottom\n[노트] speaker note"
    assert pages[1].page == 2
    assert pages[1].needs_ocr


def test_pptx_slide_limit():
    assert error_code(lambda: extract([blank_slides(101)], "PPT")) == "PAGE_LIMIT_EXCEEDED"


def test_png_pages_all_need_ocr():
    pages = extract([PNG, PNG], "PNG")
    assert [(p.page, p.needs_ocr) for p in pages] == [(1, True), (2, True)]


JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


def test_jpg_and_mixed_images_need_ocr():
    assert [p.needs_ocr for p in extract([JPG, PNG], "JPG")] == [True, True]
    assert len(extract([PNG, JPG], "PNG")) == 2
    assert error_code(lambda: extract([JPG] * 21, "JPG")) == "PAGE_LIMIT_EXCEEDED"
    assert error_code(lambda: extract([JPG, make_pdf([LONG])], "JPG")) == "UNSUPPORTED_FILE"


def test_png_limit_and_type():
    assert error_code(lambda: extract([PNG] * 21, "PNG")) == "PAGE_LIMIT_EXCEEDED"
    assert error_code(lambda: extract([PNG, make_pdf([LONG])], "PNG")) == "UNSUPPORTED_FILE"


def test_clean_normalizes_hangul_and_whitespace():
    nfd = "한글"  # 자모로 쪼개진 "한글"
    assert clean(f"  {nfd}\x00  \t text\r\n\r\n\n second­ ") == "한글 text\nsecond"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("짧은 글", True),
        ("스택은 나중에 넣은 데이터가 먼저 나오는 구조다. 큐는 먼저 넣은 데이터가 먼저 나온다.", False),
        ("ÀÌ°ÍÀº Å×½ºÆ®ÀÔ´Ï´Ù " * 4, True),
        (" " * 15, True),
        ("∑ α β ∫ 시그마와 그리스 문자, 적분 기호가 많은 수식 슬라이드도 정상 텍스트로 본다", False),
    ],
)
def test_needs_ocr(text, expected):
    assert needs_ocr(text) is expected

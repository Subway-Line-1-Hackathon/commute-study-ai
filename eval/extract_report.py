"""V1 쪽별 추출 확인. OpenAI를 부르지 않는다.

    .venv/bin/python -m eval.extract_report eval/data/자료1.pdf eval/data/자료2.pptx
    .venv/bin/python -m eval.extract_report eval/data/scan_*.png   # PNG는 한 묶음으로 처리

쪽별 텍스트는 eval/out/<파일명>.txt에 쓴다. 원본을 옆에 띄워 놓고 쪽 번호와 본문 누락을 대조한다.
"""

import sys
import time
from pathlib import Path

from app.errors import AIError
from app.extract import extract

OUT = Path(__file__).parent / "out"
TYPES = {".pdf": "PDF", ".pptx": "PPT", ".ppt": "PPT", ".png": "PNG"}


def report(name: str, files: list[bytes], file_type: str):
    start = time.perf_counter()
    try:
        pages = extract(files, file_type)
    except AIError as e:
        print(f"{name}: {e.code} - {e.message}")
        return
    ms = (time.perf_counter() - start) * 1000

    OUT.mkdir(exist_ok=True)
    body = "\n\n".join(f'<page n="{p.page}" ocr="{p.needs_ocr}">\n{p.text}\n</page>' for p in pages)
    (OUT / f"{name}.txt").write_text(body, encoding="utf-8")

    ocr = [p.page for p in pages if p.needs_ocr]
    chars = [len(p.text) for p in pages]
    print(f"{name}: {file_type} {len(pages)}쪽, {ms:.0f}ms, 총 {sum(chars)}자, 쪽당 최소 {min(chars)}자")
    print(f"  OCR 필요 {len(ocr)}쪽: {ocr}")


def main(paths: list[str]):
    pngs = [Path(p) for p in paths if Path(p).suffix.lower() == ".png"]
    for path in (Path(p) for p in paths if Path(p).suffix.lower() != ".png"):
        report(path.name, [path.read_bytes()], TYPES.get(path.suffix.lower(), "PDF"))
    if pngs:
        report(f"{pngs[0].stem}_외_{len(pngs) - 1}장", [p.read_bytes() for p in sorted(pngs)], "PNG")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])

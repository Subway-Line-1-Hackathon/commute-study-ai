"""V2 단원화 확인. 실제 OpenAI를 부른다 (.env의 OPENAI_API_KEY).

    set -a; source .env; set +a
    .venv/bin/python -m eval.run_ai01 eval/data/자료1.pdf
    .venv/bin/python -m eval.run_ai01 eval/data/scan_*.png   # PNG는 한 묶음으로 처리
    .venv/bin/python -m eval.run_ai01 eval/data/자료1.pdf --notes 필기1.jpg 필기2.jpg   # 필기 사진 함께

결과 전체는 eval/out/<파일명>.ai01.json에 쓴다. 단원 경계가 납득되는지 사람이 원본과 비교한다.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

from app import llm, pipelines
from app.errors import AIError

OUT = Path(__file__).parent / "out"
TYPES = {".pdf": "PDF", ".pptx": "PPT", ".png": "PNG"}
# 100만 토큰당 입력/출력 달러 (2026-11 기준). OCR 토큰도 단원화 모델 가격으로 계산하므로 상한 추정이다.
PRICES = {"gpt-6-luna": (0.10, 0.50), "gpt-6.1-terra": (1.0, 5.0), "gpt-6.1-sol": (2.0, 10.0)}


async def run(name: str, files: list[bytes], file_type: str, notes: list[bytes] = ()):
    started = time.perf_counter()
    try:
        out = await pipelines.process_files(files, file_type, llm.Deadline(pipelines.AI01_SECONDS), notes=notes)
    except AIError as e:
        print(f"{name}: {e.code} - {e.message} ({time.perf_counter() - started:.0f}초)")
        return

    OUT.mkdir(exist_ok=True)
    (OUT / f"{name}.ai01.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    meta = out["meta"]
    price_in, price_out = PRICES.get(meta["model"], (0, 0))
    cost = (meta["inputTokens"] * price_in + meta["outputTokens"] * price_out) / 1_000_000
    print(f"{name}: {out['pageCount']}쪽, {meta['elapsedMs'] / 1000:.0f}초, 시도 {meta['attempts']}회, "
          f"토큰 {meta['inputTokens']}/{meta['outputTokens']}, 약 ${cost:.3f} 이하")
    for c in out["categories"]:
        print(f"  {c['order']:>2}. {c['pageStart']:>3}~{c['pageEnd']:<3}쪽  {c['name']}  필기 {c.get('notePages', [])}")


async def main(args: list[str]):
    # 자료 여러 개를 한 이벤트 루프에서 돌린다. OpenAI 클라이언트가 처음 만든 루프에 묶이기 때문이다.
    paths, _, note_paths = (args + ["--notes"])[: args.index("--notes") if "--notes" in args else len(args)], None, []
    if "--notes" in args:
        note_paths = args[args.index("--notes") + 1 :]
    notes = [Path(p).read_bytes() for p in note_paths]
    pngs = sorted(Path(p) for p in paths if Path(p).suffix.lower() == ".png")
    for path in (Path(p) for p in paths if Path(p).suffix.lower() != ".png"):
        name = path.name + (f"+필기{len(notes)}" if notes else "")
        await run(name, [path.read_bytes()], TYPES.get(path.suffix.lower(), "PDF"), notes)
    if pngs:
        await run(f"{pngs[0].stem}_외_{len(pngs) - 1}장", [p.read_bytes() for p in pngs], "PNG")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1:]))

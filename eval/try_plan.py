"""출제 계획(포인트 추출) 실험. 실제 OpenAI를 부른다. 제품 코드에는 아직 연결하지 않았다.

    set -a; source .env; set +a
    .venv/bin/python -m eval.try_plan eval/out/자료.pdf.ai01.json 5 1

단원마다 포인트를 뽑고, 근거 대조를 통과한 포인트 수로 만들 수 있는 문제 수를 계산한다.
포인트 목록은 eval/out/<자료>.u<order>.plan.md로 쓴다.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app import llm
from app.validate import _evidence_page, normalize
from eval.run_ai01 import OUT, PRICES

PROMPT = (Path(__file__).parent.parent / "app" / "prompts" / "AI-02-PLAN.md").read_text(encoding="utf-8")
MAX_QUESTIONS = 40


class Point(BaseModel):
    statement: str
    source_page: int
    evidence_quote: str
    importance: Literal["core", "minor"]
    types: list[Literal["OX", "MC", "SHORT"]]


class PlanOut(BaseModel):
    points: list[Point]


def capacity(points: list[Point]) -> int:
    """핵심 포인트는 고른 유형마다 하나씩(최대 3), 보조 포인트는 한 유형으로 묻는다."""
    return min(MAX_QUESTIONS, sum(min(len(p.types), 3 if p.importance == "core" else 1) for p in points))


async def run(path: Path, order: int):
    data = json.loads(path.read_text(encoding="utf-8"))
    cat = next(c for c in data["categories"] if c["order"] == order)
    pages = {p["page"]: p["text"] for p in data["pages"] if cat["pageStart"] <= p["page"] <= cat["pageEnd"]}
    document = "\n".join(f'<page n="{n}">\n{t}\n</page>' for n, t in pages.items())
    usage = llm.Usage(model=llm.MODELS["AI02"])
    started = time.perf_counter()
    out = await llm.parse(
        "AI02",
        instructions=PROMPT,
        input=f"단원: {cat['name']}\n<document>\n{document}\n</document>",
        schema=PlanOut,
        deadline=llm.Deadline(60),
        usage=usage,
        max_output_tokens=12000,
        cache_key=f"category-{order}",
    )
    norm_pages = {p: normalize(t) for p, t in pages.items()}
    valid = [p for p in out.points if _evidence_page(p.evidence_quote, p.source_page, norm_pages)]
    by_type = {t: sum(t in p.types for p in valid) for t in ("OX", "MC", "SHORT")}
    price_in, price_out = PRICES.get(usage.model, (0, 0))
    cost = (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000
    chars = sum(len(t) for t in pages.values())
    print(
        f"{cat['name']} ({chars}자): 포인트 {len(out.points)}개 중 근거 통과 {len(valid)}개 "
        f"(핵심 {sum(p.importance == 'core' for p in valid)}), 유형별 {by_type}, "
        f"만들 수 있는 문제 {capacity(valid)}개, {time.perf_counter() - started:.0f}초, ${cost:.3f}"
    )
    lines = [f"# {cat['name']} ({chars}자) 출제 포인트", ""]
    for i, p in enumerate(out.points, 1):
        ok = "" if p in valid else " (근거 불일치)"
        lines.append(f"{i}. [{p.importance}/{','.join(p.types)}] {p.statement} ({p.source_page}쪽){ok}")
    OUT.mkdir(exist_ok=True)
    (OUT / f"{path.name.removesuffix('.ai01.json')}.u{order}.plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main(path: str, orders: list[int]):
    for order in orders:
        await run(Path(path), order)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], [int(o) for o in sys.argv[2:]]))

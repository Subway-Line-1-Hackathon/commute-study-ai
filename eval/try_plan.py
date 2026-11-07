"""출제 계획(포인트 추출) 실험. 실제 OpenAI를 부른다. 제품 코드에는 아직 연결하지 않았다.

    set -a; source .env; set +a
    .venv/bin/python -m eval.try_plan eval/out/자료.pdf.ai01.json 5 1

단원마다 포인트를 뽑고, 검증을 통과한 포인트로 만들 수 있는 문제 수를 계산한다.
포인트 목록은 eval/out/<자료>.u<order>.plan.md로 쓴다.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

from app import llm
from app.pipelines import AI02_PLAN_MAX_OUTPUT, AI02_PLAN_PROMPT as PROMPT
from app.schemas import PlanOut
from app.validate import capacity, check_points
from eval.run_ai01 import OUT, PRICES


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
        max_output_tokens=AI02_PLAN_MAX_OUTPUT,
        cache_key=f"category-{order}",
    )
    valid = check_points(out.points, pages)
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
        ok = "" if any(v.statement == p.statement for v in valid) else " (탈락: 근거 불일치·자료 문구·중복)"
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

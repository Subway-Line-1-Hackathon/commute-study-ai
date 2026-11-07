"""AI-02 사전 생성 실측. 실제 OpenAI를 부른다 (.env의 OPENAI_API_KEY).

    set -a; source .env; set +a
    .venv/bin/python -m eval.run_ai02 eval/out/자료.pdf.ai01.json 3 5   # 단원 order 3, 5

run_ai01이 만든 단원화 결과에서 단원을 골라 40문제(O/X 14, 객관식 13, 단답 13, 중 난이도)를 요청한다.
문제는 eval/out/<자료>.u<order>.ai02.csv로 쓴다. 라벨 칸에 사람이 치명/경미/정상을 적는다.
"""

import asyncio
import csv
import json
import sys
import time
from pathlib import Path

from app import pipelines
from app.errors import AIError
from app.schemas import GenerateQuestionsRequest
from eval.run_ai01 import OUT, PRICES

SPECS = [("OX", 14), ("MC", 13), ("SHORT", 13)]


async def run(path: Path, order: int):
    data = json.loads(path.read_text(encoding="utf-8"))
    cat = next(c for c in data["categories"] if c["order"] == order)
    pages = [p for p in data["pages"] if cat["pageStart"] <= p["page"] <= cat["pageEnd"]]
    req = GenerateQuestionsRequest(
        category={"categoryId": order, "name": cat["name"], "pages": pages},
        requests=[{"type": t, "difficulty": 2, "count": n} for t, n in SPECS],
    )
    name = f"{path.name.removesuffix('.ai01.json')}.u{order}"
    chars = sum(len(p["text"]) for p in pages)
    started = time.perf_counter()
    try:
        out = await pipelines.generate_questions(req)
    except AIError as e:
        print(f"{name} ({cat['name']}, {chars}자): {e.code} ({time.perf_counter() - started:.0f}초)")
        return

    meta = out["meta"]
    price_in, price_out = PRICES.get(meta["model"], (0, 0))
    cost = (meta["inputTokens"] * price_in + meta["outputTokens"] * price_out) / 1_000_000
    print(
        f"{name} ({cat['name']}, {chars}자): {len(out["questions"])}문제 (capacity {out["capacity"]}), {meta['elapsedMs'] / 1000:.0f}초, "
        f"호출 {meta['attempts']}회, ${cost:.3f}, 탈락 {out['rejected']}, 부족 {out['shortfall']}"
    )
    answers = [q["answer"] for q in out["questions"] if q["type"] == "MC"]
    print(f"  객관식 정답 번호 분포: {[answers.count(str(i)) for i in range(1, 5)]}")

    OUT.mkdir(exist_ok=True)
    with (OUT / f"{name}.ai02.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["유형", "지문", "보기", "정답", "정답목록", "해설", "근거쪽", "근거문장", "개념태그", "라벨"])
        for q in out["questions"]:
            w.writerow([
                q["type"], q["stem"], " / ".join(q["choices"]), q["answer"], " / ".join(q["acceptedAnswers"]),
                q["explanation"], q["sourcePage"], q["evidenceQuote"], ", ".join(q["conceptTags"]), "",
            ])


async def main(path: str, orders: list[int]):
    # 한 이벤트 루프에서 차례로 돌린다. 동시에 돌리면 시간을 따로 잴 수 없다.
    for order in orders:
        await run(Path(path), order)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], [int(o) for o in sys.argv[2:]]))

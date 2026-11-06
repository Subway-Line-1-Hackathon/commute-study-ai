"""AI-03·05·06 평가 사례 실행. 실제 OpenAI를 부른다 (.env의 OPENAI_API_KEY).

    set -a; source .env; set +a
    .venv/bin/python -m eval.run_cases judge      # 단답 판정 40건, 예상 판정과 비교
    .venv/bin/python -m eval.run_cases tutor      # AI 질문 13개, 답변을 사람이 확인
    .venv/bin/python -m eval.run_cases feedback   # 피드백 6개, 문장을 사람이 확인

자동으로 볼 수 있는 것(정확도, 길이, 이모지, 시간)만 표시한다. 내용 판단은 사람이 한다.
결과 전체는 eval/out/cases_<종류>.md에 쓴다.
"""

import asyncio
import csv
import json
import re
import sys
import time
from pathlib import Path

from app import llm, pipelines
from app.errors import AIError
from app.schemas import FeedbackRequest, JudgeShortRequest, TutorAskRequest
from eval.run_ai01 import OUT

CASES = Path(__file__).parent / "cases"
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def flags(text: str, limit: int) -> str:
    found = []
    if len(text) > limit:
        found.append(f"{len(text)}자")
    if EMOJI.search(text):
        found.append("이모지")
    if "!" in text:
        found.append("느낌표")
    if re.search(r"\*\*|^#|^- ", text, re.M):
        found.append("마크다운")
    return ", ".join(found) or "-"


async def judge() -> list[str]:
    rows = list(csv.DictReader((CASES / "judge_cases.csv").open(encoding="utf-8")))
    lines = ["| id | 분류 | 학생 답 | 예상 | 결과 | 시간 | 이유 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    hits, slow = 0, 0
    for r in rows:
        req = JudgeShortRequest(stem=r["문제"], accepted_answers=r["정답목록"].split("|"), explanation=r["해설"], user_answer=r["학생답"])
        started = time.perf_counter()
        try:
            out = await pipelines.judge_short(req)
            got, reason = ("정답" if out["correct"] else "오답"), out["reason"]
        except AIError as e:
            got, reason = e.code, ""
        sec = time.perf_counter() - started
        hits += got == r["예상판정"]
        slow += sec > 3
        mark = "" if got == r["예상판정"] else " ✗"
        lines.append(f"| {r['id']} | {r['분류']} | {r['학생답']} | {r['예상판정']} | {got}{mark} | {sec:.1f}초 | {reason} |")
    print(f"단답 판정: {hits}/{len(rows)} 일치, 3초 초과 {slow}건")
    return [f"일치 {hits}/{len(rows)}, 3초 초과 {slow}건", "", *lines]


async def tutor() -> list[str]:
    data = json.loads((CASES / "tutor_cases.json").read_text(encoding="utf-8"))
    lines = []
    for c in data["cases"]:
        req = TutorAskRequest(category=data["category"], question=data["question"], message=c["message"])
        usage = llm.Usage(model=llm.MODELS["AI05"])
        started, first, text = time.perf_counter(), None, ""
        try:
            async for d in pipelines.tutor_ask(req, usage):
                first = first or time.perf_counter() - started
                text += d
        except AIError as e:
            text += f" [{e.code}]"
        total = time.perf_counter() - started
        print(f"AI 질문 {c['id']:>2} ({c['kind']}): 첫 글자 {first or 0:.1f}초, 전체 {total:.1f}초, 확인 {flags(text, 400)}")
        lines += [
            f"### {c['id']}. {c['kind']}: {c['message']}",
            f"- 기대: {c['expect']}",
            f"- 첫 글자 {first or 0:.1f}초, 전체 {total:.1f}초, 자동 확인: {flags(text, 400)}",
            "",
            f"> {text}",
            "",
        ]
    return lines


async def feedback() -> list[str]:
    data = json.loads((CASES / "feedback_cases.json").read_text(encoding="utf-8"))
    lines = []
    for c in data["cases"]:
        started = time.perf_counter()
        try:
            text = (await pipelines.session_feedback(FeedbackRequest(**c["request"])))["feedback"]
        except AIError as e:
            text = f"[{e.code}]"
        sec = time.perf_counter() - started
        print(f"피드백 {c['id']} ({c['kind']}): {sec:.1f}초, 확인 {flags(text, 150)}")
        lines += [f"### {c['id']}. {c['kind']}", f"- 기대: {c['expect']}", f"- {sec:.1f}초, 자동 확인: {flags(text, 150)}", "", f"> {text}", ""]
    return lines


async def main(kind: str):
    lines = await {"judge": judge, "tutor": tutor, "feedback": feedback}[kind]()
    OUT.mkdir(exist_ok=True)
    (OUT / f"cases_{kind}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("judge", "tutor", "feedback"):
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))

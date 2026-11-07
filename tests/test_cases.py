"""평가 사례 파일이 API 요청 형식에 맞는지 확인한다. 실제 API는 부르지 않는다."""

import csv
import json
from pathlib import Path

from app.schemas import FeedbackRequest, JudgeShortRequest, TutorAskRequest

CASES = Path(__file__).parent.parent / "eval" / "cases"


def test_judge_cases():
    rows = list(csv.DictReader((CASES / "judge_cases.csv").open(encoding="utf-8")))
    assert len(rows) == 40
    for r in rows:
        JudgeShortRequest(stem=r["문제"], accepted_answers=r["정답목록"].split("|"), explanation=r["해설"], user_answer=r["학생답"])
        assert r["예상판정"] in ("정답", "오답")


def test_tutor_cases():
    data = json.loads((CASES / "tutor_cases.json").read_text(encoding="utf-8"))
    assert len(data["cases"]) == 13
    for c in data["cases"]:
        TutorAskRequest(category=data["category"], question=data["question"], message=c["message"])


def test_feedback_cases():
    data = json.loads((CASES / "feedback_cases.json").read_text(encoding="utf-8"))
    assert len(data["cases"]) == 6
    for c in data["cases"]:
        FeedbackRequest(**c["request"])

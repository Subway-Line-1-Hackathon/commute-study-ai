import os

os.environ["AI_INTERNAL_KEY"] = "test-key"

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
KEY = {"X-Internal-Key": "test-key"}

CATEGORY = {"categoryId": 1, "name": "스택과 큐", "pages": [{"page": 1, "text": "스택은 LIFO다."}]}
REQUESTS = {
    "/v1/materials/process": {
        "materialId": 123,
        "fileType": "PDF",
        "kind": "LECTURE",
        "fileName": "a.pdf",
        "fileUrls": ["https://x.supabase.co/storage/v1/object/sign/a.pdf"],
    },
    "/v1/questions/generate": {
        "category": CATEGORY,
        "requests": [{"type": "OX", "difficulty": 2, "count": 2}, {"type": "MC", "difficulty": 3, "count": 1}],
    },
    "/v1/answers/judge-short": {
        "stem": "FIFO 구조는?",
        "acceptedAnswers": ["큐"],
        "explanation": "큐는 FIFO다.",
        "userAnswer": "대기열",
    },
    "/v1/answers/grade-essay": {
        "stem": "스택과 큐의 차이는?",
        "rubric": ["스택은 LIFO", "큐는 FIFO"],
        "explanation": "...",
        "userAnswer": "스택은 나중 것이 먼저 나온다.",
    },
    "/v1/tutor/ask": {
        "category": CATEGORY,
        "question": {"stem": "FIFO 구조는?", "choices": ["스택", "큐"], "answer": "2", "explanation": "..."},
        "message": "큐는 어디에 써요?",
    },
    "/v1/sessions/feedback": {
        "sessionId": 1,
        "mode": "CURRICULUM",
        "total": 15,
        "answered": 15,
        "correct": 12,
    },
}
EXPECTED_KEYS = {
    "/v1/materials/process": {"pageCount", "pages", "categories", "meta"},
    "/v1/questions/generate": {"questions", "shortfall", "capacity", "rejected", "meta"},
    "/v1/answers/judge-short": {"correct", "reason"},
    "/v1/answers/grade-essay": {"correct", "score", "points", "feedback"},
    "/v1/sessions/feedback": {"feedback"},
}


def events(text: str) -> list[str]:
    return [line.removeprefix("event: ") for line in text.splitlines() if line.startswith("event: ")]


def test_health_needs_no_key():
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("path", REQUESTS)
def test_wrong_key_is_401(path):
    r = client.post(path, json=REQUESTS[path], headers={"X-Internal-Key": "wrong"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize("path", EXPECTED_KEYS)
def test_mock_response_shape(path):
    r = client.post(path, json=REQUESTS[path], headers=KEY)
    assert r.status_code == 200
    assert set(r.json()) == EXPECTED_KEYS[path]


def test_invalid_request_is_400():
    r = client.post("/v1/answers/judge-short", json={"stem": "x"}, headers=KEY)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_unknown_field_is_400():
    body = {**REQUESTS["/v1/sessions/feedback"], "sesionId": 1}
    assert client.post("/v1/sessions/feedback", json=body, headers=KEY).status_code == 400


def test_pdf_with_two_urls_is_400():
    body = {**REQUESTS["/v1/materials/process"]}
    body["fileUrls"] = body["fileUrls"] * 2
    assert client.post("/v1/materials/process", json=body, headers=KEY).status_code == 400


def test_generate_returns_requested_counts():
    questions = client.post("/v1/questions/generate", json=REQUESTS["/v1/questions/generate"], headers=KEY).json()[
        "questions"
    ]
    assert [(q["type"], q["difficulty"]) for q in questions] == [("OX", 2), ("OX", 2), ("MC", 3)]
    assert len({q["stem"] for q in questions}) == 3


def test_generate_over_40_is_400():
    body = {"category": CATEGORY, "requests": [{"type": "OX", "difficulty": 1, "count": 41}]}
    assert client.post("/v1/questions/generate", json=body, headers=KEY).status_code == 400


def test_mock_error_header():
    r = client.post(
        "/v1/materials/process",
        json=REQUESTS["/v1/materials/process"],
        headers={**KEY, "X-Mock-Error": "TEXT_EXTRACTION_FAILED"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "TEXT_EXTRACTION_FAILED"


def test_unknown_mock_error_is_400():
    r = client.post(
        "/v1/answers/judge-short",
        json=REQUESTS["/v1/answers/judge-short"],
        headers={**KEY, "X-Mock-Error": "NOPE"},
    )
    assert r.status_code == 400


def test_tutor_streams_delta_then_done():
    r = client.post("/v1/tutor/ask", json=REQUESTS["/v1/tutor/ask"], headers=KEY)
    assert r.headers["content-type"].startswith("text/event-stream")
    names = events(r.text)
    assert names[-1] == "done"
    assert set(names[:-1]) == {"delta"}


def test_tutor_error_keeps_partial_text():
    r = client.post("/v1/tutor/ask", json=REQUESTS["/v1/tutor/ask"], headers={**KEY, "X-Mock-Error": "LLM_TIMEOUT"})
    assert events(r.text) == ["delta", "error"]
    assert '"code": "LLM_TIMEOUT"' in r.text


def test_mock_source_pages_stay_in_category():
    # 백엔드는 단원 쪽 범위 밖의 sourcePage를 버린다 (#38)
    category = {"categoryId": 2, "name": "큐", "pages": [{"page": 4, "text": "큐는 FIFO 구조다."}, {"page": 5, "text": "덱은 양쪽 끝에서 넣고 뺀다."}]}
    body = {"category": category, "requests": [{"type": "SHORT", "difficulty": 1, "count": 3}]}
    questions = client.post("/v1/questions/generate", json=body, headers=KEY).json()["questions"]
    assert [q["sourcePage"] for q in questions] == [4, 5, 4]
    assert questions[1]["evidenceQuote"] == "덱은 양쪽 끝에서 넣고 뺀다."

# AI 서버 API 명세 v0.3

백엔드가 호출하는 내부 API다. 합의 전 초안이다.

- 기준: 기능명세서 5장 AI-01~06 (2026-11-05 저장본), 백엔드 초기 스키마 `V1__init_schema.sql` (`Feat/#3` 브랜치, `develop` 머지 전)
- 기능정의서 (3)~(7)과 백엔드 개발 명세서를 아직 못 봐서 필드가 바뀔 수 있다.

**v0.2에서 바뀐 것 (기능명세서 11-05 수정본 반영):**

| 항목 | v0.2 | v0.3 | 백엔드 영향 |
| --- | --- | --- | --- |
| AI-02 서버 시간 상한 | 80초 | 55초 (사전 생성·추가 생성 모두). 백엔드 타임아웃은 둘 다 60초 | 있음. 추가 생성이 60초 이내로 정해졌다. AI 서버 코드는 다음 구현 PR에서 바꾼다 |
| AI-02 사전 생성 예시 | 중 난이도 30문제 | 40문제, O/X·객관식·단답 14·13·13 | 요청 내용만 바뀐다. 요청 형식은 같다 |
| AI-02 사전 생성 부족분 | 커리큘럼 생성 때 다시 요청 | `PREPARING` 동안 바로 다시 요청 (제안, 10장) | 있음 |
| AI-04 | P1 | P2. 구현하지 않고 mock 응답만 유지 | 호출하지 않으면 된다 |
| AI-05 | - | 새 문제·유사 문제를 만들어 달라는 요청에는 응하지 않는다 | 없음 |

v0.1에서는 백엔드 스키마에 이름과 값을 맞췄다. 단원은 `category`, 객관식은 `MC`, 난이도는 정수, 자료 형식은 `PPT`, ID는 숫자다.

## 1. 공통 규칙

| 항목 | 내용 |
| --- | --- |
| 호출 주체 | 백엔드만 호출한다. 앱은 AI 서버를 직접 호출하지 않는다 |
| Base URL | `http://commute-study-ai.railway.internal:8000` (Railway 내부망) |
| 인증 | `/v1` 요청에 `X-Internal-Key` 헤더. 값은 양쪽 환경변수로 공유. `/health`는 Railway 헬스체크용이라 키가 없다 |
| mock 에러 | mock 단계에서는 `X-Mock-Error: <에러 코드>` 헤더로 실패 응답을 시험할 수 있다 |
| 형식 | JSON, UTF-8. 필드명은 camelCase. AI-05만 응답이 SSE |
| 상태 | AI 서버는 DB가 없다. 필요한 내용은 매 요청에 담아 보내고, 저장은 백엔드가 한다 |

### 호출 목록

| ID | 메서드·경로 | 호출 시점 | AI 서버 시도 | 서버 시간 상한(초) | 백엔드 타임아웃(초) |
| --- | --- | --- | --- | --- | --- |
| - | `GET /health` | 배포 확인 | - | - | 5 |
| AI-01 | `POST /v1/materials/process` | 자료 `PROCESSING` 진입 시 (M2, M5) | 2회 | 270 | 300 |
| AI-02 | `POST /v1/questions/generate` | `READY` 후 단원별 사전 생성 1회, 재출제할 문제가 부족할 때 추가 생성 | 생성 1회 + 재생성 최대 2회 | 55 | 60 |
| AI-03 | `POST /v1/answers/judge-short` | 단답이 정답 목록과 다를 때 | 1회 | 4 | 5 |
| AI-04 | `POST /v1/answers/grade-essay` (P2) | 구현하지 않음. mock 응답만 있다 | - | - | - |
| AI-05 | `POST /v1/tutor/ask` (SSE) | AI 질문 버튼·입력 시 | 1회 | 13 | 15 |
| AI-06 | `POST /v1/sessions/feedback` | 세션 종료 직후 | 1회 | 13 | 15 |

AI-02는 추가 생성이 60초 이내여야 해서(기능명세서 5장) 서버 시간 상한을 55초로 둔다. 요청 하나로 사전 생성과 추가 생성을 구분하지 않으므로 사전 생성도 55초다. 55초 안에 다 못 만든 몫은 `shortfall`로 돌아온다. 백엔드가 한 번 호출하면 AI 서버가 안에서 10문제씩 나눠 동시에 만든다.

서버 시간 상한은 백엔드 타임아웃보다 짧다. 그래야 백엔드가 연결을 끊기 전에 AI 서버가 에러나 부분 결과를 돌려줄 수 있다. 시간 상한에 닿으면 남은 재시도를 하지 않는다.

### 에러 응답

```json
{ "error": { "code": "TEXT_EXTRACTION_FAILED", "message": "글자를 인식하지 못했어요. 글자가 선명한 파일로 다시 올려주세요." } }
```

`message`는 사용자에게 그대로 보여줄 수 있는 문장이다.

| HTTP | code | 의미 | 백엔드 처리 |
| --- | --- | --- | --- |
| 400 | `INVALID_REQUEST` | 요청 형식 오류 | 호출 코드 수정 |
| 401 | `UNAUTHORIZED` | 내부 키 오류 | 환경변수 확인 |
| 422 | `UNSUPPORTED_FILE` | 열 수 없거나 지원하지 않는 파일 (구버전 .ppt 포함) | `FAILED`, 재시도 무의미 |
| 422 | `PAGE_LIMIT_EXCEEDED` | PDF·PPT 100쪽, PNG 20장 초과 | `FAILED`. 기능명세서는 용량 초과를 업로드 단계에서 거부하므로 확인 필요 (10장) |
| 422 | `TEXT_EXTRACTION_FAILED` | 텍스트 인식 실패 | `FAILED`, 재시도 무의미 |
| 502 | `FILE_FETCH_FAILED` | 파일 URL 다운로드 실패 | 서명 URL을 새로 만들어 재시도 가능 |
| 502 | `LLM_FAILED` | AI 응답 오류. 호출 목록의 시도 횟수를 다 쓴 뒤 반환 | 기능별 실패 처리 |
| 504 | `LLM_TIMEOUT` | 서버 시간 상한 초과 | 기능별 실패 처리 |

파일 크기 30MB 초과와 PNG 20장 초과는 M1대로 백엔드가 업로드 단계에서 거부한다. PDF·PPT 쪽수는 파일을 열어야 알 수 있다. 백엔드가 업로드 때 확인하지 않으면 AI 서버가 `PAGE_LIMIT_EXCEEDED`로 돌려준다. AI 서버는 어느 쪽이든 쪽수를 다시 확인한다.

### 공통 타입

```text
Page       { page: int, text: string }
UsageMeta  { model: string, inputTokens: int, outputTokens: int, elapsedMs: int, attempts: int }
```

| 필드 | 값 | 백엔드 스키마 |
| --- | --- | --- |
| `fileType` | `PDF` \| `PPT` \| `PNG` | `material_file_type` |
| `kind` | `LECTURE` \| `PAST_EXAM` | `material_kind` |
| `type` | `OX` \| `MC` \| `SHORT` \| `ESSAY` | `question_type` |
| `difficulty` | `1`(하) \| `2`(중) \| `3`(상) | `question.difficulty` |
| `mode` | `CURRICULUM` \| `REVIEW` | `session_mode` |

- 쪽 번호는 PDF 뷰어 기준이고 1부터 시작한다. PPT는 슬라이드 번호, PNG는 업로드 순서를 쓴다.
- `PPT`는 .pptx만 처리한다. 구버전 .ppt는 읽을 라이브러리가 없어서 `UNSUPPORTED_FILE`이다.

---

## 2. AI-01 단원화·요약본 생성

`POST /v1/materials/process`

```json
{
  "materialId": 123,
  "fileType": "PDF",
  "kind": "LECTURE",
  "fileName": "자료구조_3주차.pdf",
  "fileUrls": ["https://<project>.supabase.co/storage/v1/object/sign/..."]
}
```

- `fileUrls`: PDF·PPT는 1개, PNG는 최대 20개를 쪽 순서대로 보낸다.
- `kind`: `PAST_EXAM`(족보)도 MVP에서는 `LECTURE`와 같이 단원화한다. 족보로 문제를 만드는 방식이 정해지면 바꾼다.

**응답 200**

```json
{
  "pageCount": 2,
  "pages": [
    { "page": 1, "text": "스택은 나중에 넣은 데이터가 먼저 나오는 구조다." },
    { "page": 2, "text": "큐는 먼저 넣은 데이터가 먼저 나오는 구조다." }
  ],
  "categories": [
    {
      "order": 1,
      "name": "스택과 큐",
      "pageStart": 1,
      "pageEnd": 2,
      "summary": "스택은 LIFO, 큐는 FIFO 구조다. ..."
    }
  ],
  "meta": { "model": "gpt-6.1-terra", "inputTokens": 35000, "outputTokens": 3200, "elapsedMs": 62000, "attempts": 1 }
}
```

| 응답 | 저장 위치 |
| --- | --- |
| `pageCount` | `material.page_count` |
| `categories` | `category` |
| `pages` | 스키마에 없음 (9장 1번). AI-02와 AI-05가 다시 입력으로 쓴다. 원본 파일이 아니므로 `EXPIRED` 뒤에도 남겨야 AI 질문이 동작한다 |
| 에러 `message` | `material.error_message` |

`categories`는 1쪽부터 마지막 쪽까지 빠짐없이, 겹치지 않게 이어진다. AI 서버가 검증한 뒤 반환한다.

**상태 전이**

| 결과 | 전이 |
| --- | --- |
| 200 | M3 `PROCESSING` → `READY` |
| `UNSUPPORTED_FILE`, `PAGE_LIMIT_EXCEEDED`, `TEXT_EXTRACTION_FAILED`, `LLM_FAILED`, `LLM_TIMEOUT` | M4 `PROCESSING` → `FAILED`, `message`를 실패 사유로 표시 |
| `FILE_FETCH_FAILED` | 서명 URL을 새로 만들어 1회 다시 호출, 또 실패하면 M4 |
| 사용자 재시도 | M5 같은 파일로 1회 다시 호출 |

원본 파일이 만료되면(M6 `EXPIRED`) AI-01을 다시 호출할 수 없다. 단원·요약본은 이미 저장돼 있어 영향이 없다.

---

## 3. AI-02 문제·해설 생성

`POST /v1/questions/generate`

```json
{
  "category": {
    "categoryId": 1,
    "name": "스택과 큐",
    "pages": [
      { "page": 1, "text": "스택은 나중에 넣은 데이터가 먼저 나오는 구조다." },
      { "page": 2, "text": "큐는 먼저 넣은 데이터가 먼저 나오는 구조다." }
    ]
  },
  "requests": [
    { "type": "OX", "difficulty": 2, "count": 14 },
    { "type": "MC", "difficulty": 2, "count": 13 },
    { "type": "SHORT", "difficulty": 2, "count": 13 }
  ],
  "excludeStems": ["스택의 입출력 방식은?"]
}
```

- `excludeStems`: 이미 있는 문제의 지문. 같은 문제를 다시 만들지 않기 위해 보낸다.
- `requests`의 `count` 합계는 40 이하다. 넘으면 `INVALID_REQUEST`다.
- 사전 생성은 위 예시처럼 40문제를 O/X·객관식·단답 균등(14·13·13)으로 한 번에 요청한다 (기능명세서 4-2). 난이도 구성은 아직 정해지지 않았다 (10장). 유형·난이도 조합마다 `requests` 항목을 나누면 된다.
- 추가 생성은 이미 있는 문제의 지문을 `excludeStems`에 모두 넣고, 필요한 유형만 균등하게 요청한다.
- 논술(`ESSAY`)은 P2라 요청하지 않는다. 형식은 남겨 둔다.

**응답 200**

```json
{
  "questions": [
    {
      "type": "MC",
      "difficulty": 2,
      "stem": "다음 중 FIFO 구조는?",
      "choices": ["스택", "큐", "트리", "그래프"],
      "answer": "2",
      "acceptedAnswers": [],
      "rubric": [],
      "explanation": "큐는 먼저 넣은 데이터가 먼저 나오는 FIFO 구조다.",
      "sourcePage": 2,
      "evidenceQuote": "큐는 먼저 넣은 데이터가 먼저 나오는 구조다.",
      "conceptTags": ["큐", "FIFO"]
    }
  ],
  "shortfall": [
    { "type": "SHORT", "difficulty": 2, "count": 1 }
  ],
  "rejected": { "schema": 1, "evidence": 2, "duplicate": 0 },
  "meta": { "model": "gpt-6.1-terra", "inputTokens": 5000, "outputTokens": 9000, "elapsedMs": 40000, "attempts": 2 }
}
```

| 유형 | `choices` | `answer` | `acceptedAnswers` | `rubric` |
| --- | --- | --- | --- | --- |
| `OX` | `[]` | `"O"` 또는 `"X"` | `[]` | `[]` |
| `MC` | 4개 | 정답 번호 `"1"`~`"4"` | `[]` | `[]` |
| `SHORT` | `[]` | 대표 정답 | 표기 변형을 포함한 정답 목록 | `[]` |
| `ESSAY` | `[]` | `""` | `[]` | 채점용 핵심 포인트 3~5개 |

- 필드는 `question` 테이블 컬럼과 같다. `sourcePage`는 `source_page`에 저장한다.
- `evidenceQuote`는 AI 서버가 근거 검증에 쓴 원문 문장이다. 저장은 선택이다.
- `rubric`과 `conceptTags`는 스키마와 맞지 않는다 (9장 3·4번).
- 해설은 문제와 함께 미리 만든다. 사용자가 풀 때 추가 AI 호출이 없다 (LN-05).
- 서버 안에서 형식 오류와 근거 불일치 문제를 버리고, 부족분만 최대 2회 재생성한다.
- 그래도 모자라면 있는 문제와 `shortfall`을 200으로 반환한다. 문제가 0개면 `LLM_FAILED`다.

**실패 처리**

| 시점 | 백엔드 처리 |
| --- | --- |
| 사전 생성에서 `shortfall` 또는 실패 | 단원은 `PREPARING`으로 두고 모자란 몫만 바로 다시 요청한다. 2번 더 해도 모자랄 때의 처리는 정해지지 않았다 (10장) |
| 추가 생성에서 `shortfall` | 있는 문제로만 커리큘럼을 만든다 |
| 추가 생성 실패이고 쓸 문제가 0개 | 커리큘럼을 만들지 않고 실패를 안내한다. 사용자가 다시 시도할 수 있다 |

- `PREPARING`인 단원은 선택할 수 없으므로 추가 생성이 겹치지 않는다.
- 자료가 `EXPIRED`이면 추가 생성을 하지 않는다 (기능명세서 M6). AI 서버는 원본 파일 대신 `pages`를 쓰므로 기술적으로는 가능하지만, 기획 결정을 따른다.

---

## 4. AI-03 단답형 판정

`POST /v1/answers/judge-short`

백엔드가 공백·대소문자를 정규화해 `acceptedAnswers`와 먼저 비교하고, 일치하지 않을 때만 호출한다.

```json
{
  "stem": "먼저 넣은 데이터가 먼저 나오는 자료구조는?",
  "acceptedAnswers": ["큐", "Queue"],
  "explanation": "큐는 FIFO 구조다.",
  "userAnswer": "대기열"
}
```

**응답 200**

```json
{ "correct": true, "reason": "큐를 뜻하는 다른 표현이에요." }
```

타임아웃이나 에러면 앱에 대표 정답을 보여주고 자가 판정 화면을 띄운다. 자가 판정 결과도 정답률에 포함하고, 자가 판정 여부는 응답 기록에 함께 저장한다 (9장 5번).

---

## 5. AI-04 논술형 채점 (P2)

기능명세서에서 P2(3주 범위 밖)로 바뀌어 구현하지 않는다. 아래 형식의 mock 응답만 남겨 둔다.

`POST /v1/answers/grade-essay`

```json
{
  "stem": "스택과 큐의 차이를 설명하시오.",
  "rubric": ["스택은 LIFO", "큐는 FIFO", "활용 사례"],
  "explanation": "...",
  "userAnswer": "스택은 나중에 넣은 게 먼저 나오고 큐는 반대다."
}
```

**응답 200**

```json
{
  "correct": true,
  "score": 0.67,
  "points": [
    { "point": "스택은 LIFO", "met": true, "reason": "동작을 정확히 설명했어요." },
    { "point": "큐는 FIFO", "met": true, "reason": "반대라고 설명했어요." },
    { "point": "활용 사례", "met": false, "reason": "사례가 없어요." }
  ],
  "feedback": "두 구조의 차이는 맞게 설명했어요. 활용 사례를 덧붙이면 좋아요."
}
```

- `score`는 충족한 핵심 포인트의 비율이고 `attempt.score`에 저장한다. `correct`는 `score`가 0.6 이상이면 `true`다.
- 구현하게 되면 실패한 답안은 "나중에 확인"으로 저장한다 (기능명세서 5장).

---

## 6. AI-05 AI 질문

`POST /v1/tutor/ask`, 응답 `text/event-stream`

```json
{
  "category": {
    "categoryId": 1,
    "name": "스택과 큐",
    "pages": [{ "page": 1, "text": "..." }]
  },
  "question": {
    "stem": "다음 중 FIFO 구조는?",
    "choices": ["스택", "큐", "트리", "그래프"],
    "answer": "2",
    "explanation": "...",
    "userAnswer": "1"
  },
  "history": [
    { "role": "user", "content": "왜 스택은 아니에요?" },
    { "role": "assistant", "content": "스택은 ..." }
  ],
  "message": "그럼 큐는 어디에 써요?"
}
```

꼬리질문 버튼은 고정 문구를 `message`로 보낸다. 자유 질문과 호출 방식이 같다. `history`는 최근 3왕복만 보낸다.

**이벤트**

```text
event: delta
data: {"text":"큐는 "}

event: delta
data: {"text":"대기열 처리에 써요."}

event: done
data: {"meta":{"model":"...","inputTokens":5000,"outputTokens":120,"elapsedMs":3100,"attempts":1}}
```

실패하면 `event: error`에 공통 에러 형식을 담아 보낸다.

- 횟수 제한(하루 20회, 한국 시간 자정 초기화)은 백엔드가 `ai_question_usage`로 관리한다. `done`을 받았을 때만 차감하면 "실패 시 차감하지 않음"이 지켜진다.
- 사용자당 진행 중인 질문은 1개로 제한한다. 그래야 동시 요청으로 20회를 넘지 않고, `done`을 못 받은 호출이 겹쳐 쌓이지 않는다.
- `delta` 뒤에 `error`가 오면 앱은 나온 내용을 남기고 다시 시도 버튼을 표시한다.
- 답변은 단원 자료를 기준으로 하고, 자료에 없는 내용은 없다고 밝힌다.
- 새 문제나 유사 문제를 만들어 달라는 요청에는 응하지 않고 개념 설명으로 돌린다 (기능명세서 4-4).
- 자료가 `EXPIRED`여도 `category.pages`를 보내면 동작한다.

---

## 7. AI-06 통학 후 피드백

`POST /v1/sessions/feedback`

```json
{
  "sessionId": 1,
  "mode": "CURRICULUM",
  "total": 15,
  "answered": 15,
  "correct": 12,
  "correctConcepts": ["스택", "큐"],
  "wrongItems": [
    { "stem": "다음 중 FIFO 구조는?", "conceptTags": ["FIFO"] }
  ]
}
```

- 호출 시점은 세션이 `COMPLETED` 또는 `ENDED`가 된 직후다. `answered`가 0이거나 S6 '새로 시작'으로 끝난 세션은 호출하지 않는다. 사용자가 S-50을 보지 않기 때문이다. S7 자정 자동 종료도 같은 이유로 호출하지 않는 것을 제안한다 (10장). `answered`가 0이면 AI 서버는 `400 INVALID_REQUEST`를 돌려준다.
- 세션은 최대 30문제라 `wrongItems`도 30개 이하다. AI 서버는 앞의 20개만 쓴다.
- `correctConcepts`, `wrongItems[].conceptTags`는 문제의 개념 태그에서 모은다 (9장 3번).

**응답 200**

```json
{ "feedback": "스택과 큐의 기본 개념은 잘 이해했어요. FIFO가 헷갈렸으니 다음에 다시 풀어보면 좋아요." }
```

앱은 S-50에 숫자를 먼저 표시하고, 피드백이 오면 문장을 채운다. 실패하면 숫자만 두고 재시도하지 않는다.

---

## 8. 지금 없는 API

| 기능 | 이유 |
| --- | --- |
| 커리큘럼 구성 (CR-03) | 문제은행에서 고르는 작업이라 AI 호출이 필요 없다. 부족할 때만 AI-02를 호출한다 |
| 복습 커리큘럼 (CR-04) | 푼 문제에서 고르므로 AI 호출이 없다 |
| 유사 문제 | 기획에서 구현하지 않기로 했다 |
| 오답 분석 | P2 |
| 듣기 (LN-08) | 기기 내장 TTS라 AI 호출이 없다. 요약본·문제·해설은 소리 내어 읽기 좋은 문장으로 만든다 |
| 족보로 문제 만들기 | 스키마에 `PAST_EXAM`이 있지만 활용 방식이 정해지지 않았다. 지금은 AI-01이 `kind`만 받는다 |

---

## 9. 백엔드 스키마와 맞출 것

`V1__init_schema.sql` 기준이다. 이미 적용된 마이그레이션은 고칠 수 없으므로, 합의되면 V2 마이그레이션으로 반영한다.

| # | 차이 | 제안 |
| --- | --- | --- |
| 1 | AI-01 쪽 텍스트(`pages`)를 저장할 곳이 없다 | `material_page(material_id, page, text)` 테이블 |
| 2 | 단원별 문제 생성 상태가 없다 | `category`에 문제 상태 컬럼. 값은 기능명세서 3-1의 `PREPARING`, `AVAILABLE` |
| 3 | 개념 태그 컬럼이 없다 | `question.concept_tags jsonb`. AI-06 피드백에 쓴다 |
| 4 | `rubric`이 `text`다 | 논술이 P2라 급하지 않다. 바꾼다면 `jsonb` |
| 5 | 자가 판정 여부를 저장할 곳이 없다 | `attempt.self_judged boolean` |
| 6 | `difficulty` 값의 의미가 정해지지 않았다 | 1 하, 2 중, 3 상 |
| 7 | `ai_question_usage`가 P1로 표시돼 있다 | 기능명세서 4-4에서 하루 20회가 P0로 정해졌다. 스키마 주석을 P0로 |
| 8 | 지원 형식 `PPT` | 업로드 단계에서 .pptx만 받는다 |
| 9 | 자료 상태에 `EXPIRED`가 생겼다 | `material_status`에 추가. 원본 파일만 지우고 1번 쪽 텍스트는 남긴다 |

## 10. 팀 합의가 필요한 것

### 기능명세서에 아직 없는 제안

11-05 수정본에서 AI 질문 하루 20회와 사전 생성 단원 상태가 반영됐다. 나머지 빈도·실패 시 수정안은 아직 반영되지 않았다.

| 기능 | 이 명세의 제안 |
| --- | --- |
| 공통 | 기능별 시도 횟수와 서버 시간 상한 (1장 호출 목록) |
| AI-01 | 텍스트 인식 실패·파일 오류·쪽수 초과는 재시도 없이 `FAILED` |
| AI-02 | 사전 생성이 모자라면 `PREPARING` 동안 모자란 몫만 바로 2번까지 다시 요청 |
| AI-02 | 단원·난이도별 60문제 상한. 추가 생성이 끝없이 늘지 않게 막는다 |
| AI-03 | 자가 판정을 정답률에 포함 |
| AI-05 | 한국 시간 자정 초기화, 사용자당 동시 1개 |
| AI-06 | 푼 문제가 0개이거나 '새로 시작'(S6)·자정 자동 종료(S7)로 끝난 세션은 호출하지 않음 |

### 기능명세서에서 정해지지 않은 것

| 질문 | 대상 | 영향 |
| --- | --- | --- |
| 사전 생성 40문제의 난이도 구성 (모두 중인지, 하·중·상을 섞는지) | 기획 | 사용자가 고른 난이도의 문제가 없으면 매번 추가 생성(대기 화면)이 생긴다 |
| 사전 생성을 다시 요청해도 40문제가 안 되면 `AVAILABLE`로 둘지 | 기획·백엔드 | 진도 = 푼 문제 ÷ 40 계산 |
| PDF·PPT 쪽수 초과를 업로드 단계에서 거부할지, AI 서버 응답으로 `FAILED` 처리할지 | 백엔드 | 3-1은 용량 초과를 업로드 단계에서 거부한다. 쪽수를 세려면 백엔드가 파일을 열어야 한다 |
| `EXPIRED` 뒤에도 쪽 텍스트(9장 1번)를 남기는지 | 백엔드 | 지우면 만료된 자료의 AI 질문이 동작하지 않는다 |

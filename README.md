# commute-study-ai

통학 AI 학습 앱의 AI 서버다. 백엔드만 호출하는 내부 API이고, DB 없이 요청을 받아 결과를 돌려준다.

기본은 **mock**이다. 모든 API가 [API 명세](docs/API_명세.md) 형식의 고정 응답을 돌려준다. `AI_MOCK=false`로 두면 구현된 기능부터 실제 AI를 쓴다.

| 기능 | 상태 |
| --- | --- |
| AI-01 단원화·요약 | 구현 (`AI_MOCK=false`일 때) |
| AI-02 문제·해설 생성 | 구현 (`AI_MOCK=false`일 때) |
| AI-03 단답형 판정 | 구현 (`AI_MOCK=false`일 때) |
| AI-06 통학 후 피드백 | 구현 (`AI_MOCK=false`일 때) |
| AI-05 AI 질문 | mock |
| AI-04 논술형 채점 | P2라 구현하지 않음. mock만 유지 |

| 환경변수 | 설명 |
| --- | --- |
| `AI_INTERNAL_KEY` | 백엔드와 같은 내부 키. 필수 |
| `AI_MOCK` | 기본 `true`. 실제 AI를 쓰려면 `false` |
| `OPENAI_API_KEY` | 실제 AI를 쓸 때 필요 |
| `SUPABASE_HOST` | 파일 URL로 허용할 호스트. 실제 AI를 쓸 때 필요 |
| `MODEL_AI01`, `EFFORT_AI01` 등 | 기능별 모델·추론 강도 변경 (선택) |

## 실행

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
set -a; source .env; set +a
.venv/bin/uvicorn app.main:app --reload
```

API 문서: http://localhost:8000/docs

## 테스트

```bash
.venv/bin/python -m pytest
```

push와 PR마다 GitHub Actions가 같은 테스트를 돌린다. 테스트는 OpenAI API를 부르지 않는다.

## Docker

```bash
docker build -t commute-study-ai .
docker run --rm -p 8000:8000 -e AI_INTERNAL_KEY=change-me commute-study-ai
```

## 브랜치

| 브랜치 | 역할 |
| --- | --- |
| `develop` | 기본 브랜치. 이슈별 `{Type}/#{번호}` 브랜치의 PR이 모인다 |
| `main` | Railway 배포 브랜치. `develop` → `main` PR로만 바뀐다 |

## 백엔드 연동

- 모든 `/v1` 요청에 `X-Internal-Key` 헤더가 필요하다. `/health`는 필요 없다.
- 실패 처리를 시험하려면 `X-Mock-Error` 헤더에 에러 코드를 넣는다. 예: `X-Mock-Error: LLM_FAILED`. AI-05는 글자 한 조각 뒤에 `error` 이벤트를 보낸다. `AI_MOCK=false`이면 이 헤더는 무시된다.
- mock 응답은 `fixtures/`에 있다. AI-02는 요청한 유형·난이도·개수만큼 문제를 만들어 돌려준다.

## 문서

| 문서 | 내용 |
| --- | --- |
| [API_명세.md](docs/API_명세.md) | 백엔드와 맞추는 요청·응답 |
| [개발계획.md](docs/개발계획.md) | 구조, 단계, 예산, 일정 |
| [기능별_개발계획.md](docs/기능별_개발계획.md) | 기능별 처리 단계, 프롬프트, 검증, 테스트 |

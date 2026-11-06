"""LLM 출력 검증과 보정. LLM을 부르지 않는 순수 함수만 둔다."""

import difflib
import random
import re
import unicodedata

from app.schemas import CategoryDraft, EssayDraft, MCDraft, OXDraft, QuestionBase, ShortDraft


class InvalidOutput(ValueError):
    """재시도할 때 프롬프트 끝에 붙일 이유를 담는다."""


# ---------- AI-01 단원 ----------

MAX_CATEGORIES = 20
MIN_CATEGORY_CHARS = 1000  # 이보다 짧은 단원은 40문제를 만들기 어려워 옆 단원에 합친다 (eval/결과.md 11-06)
MAX_SUMMARY_CHARS = 1500


def fix_categories(drafts: list[CategoryDraft], page_lengths: list[int]) -> list[dict]:
    """단원 시작 쪽만 믿고 경계는 코드가 다시 계산한다. API 응답의 categories 형태로 돌려준다."""
    count = len(page_lengths)
    starts = [d.page_start for d in drafts]
    if not 1 <= len(drafts) <= MAX_CATEGORIES:
        raise InvalidOutput(f"단원 수는 1~{MAX_CATEGORIES}개여야 한다 (현재 {len(drafts)}개)")
    if any(not 1 <= s <= count for s in starts) or len(set(starts)) != len(starts):
        raise InvalidOutput(f"page_start는 1~{count} 사이이고 서로 달라야 한다")
    for d in drafts:
        if not d.name.strip() or not d.summary.strip():
            raise InvalidOutput("모든 단원에 name과 summary가 있어야 한다")
        if len(d.summary) > MAX_SUMMARY_CHARS:
            raise InvalidOutput(f"summary는 {MAX_SUMMARY_CHARS}자 이내여야 한다")

    drafts = sorted(drafts, key=lambda d: d.page_start)
    cats = [
        {
            "name": d.name.strip(),
            "pageStart": 1 if i == 0 else d.page_start,
            "pageEnd": drafts[i + 1].page_start - 1 if i + 1 < len(drafts) else count,
            "summary": d.summary.strip(),
        }
        for i, d in enumerate(drafts)
    ]

    def chars(c: dict) -> int:
        return sum(page_lengths[c["pageStart"] - 1 : c["pageEnd"]])

    while len(cats) > 1:
        small_ones = [i for i, c in enumerate(cats) if chars(c) < MIN_CATEGORY_CHARS]
        if not small_ones:
            break
        # 가장 작은 단원을 앞뒤 중 더 작은 단원과 합친다. 한쪽으로만 합치면 작은 단원이 줄줄이 붙어 한 단원이 너무 커진다.
        small = min(small_ones, key=lambda i: chars(cats[i]))
        other = min((i for i in (small - 1, small + 1) if 0 <= i < len(cats)), key=lambda i: chars(cats[i]))
        a, b = sorted((small, other))
        keep = a if chars(cats[a]) >= chars(cats[b]) else b  # 이름은 큰 쪽을 쓴다
        cats[a : b + 1] = [
            {
                "name": cats[keep]["name"],
                "pageStart": cats[a]["pageStart"],
                "pageEnd": cats[b]["pageEnd"],
                "summary": f"{cats[a]['summary']}\n{cats[b]['summary']}",
            }
        ]

    return [{"order": i, **c} for i, c in enumerate(cats, 1)]


# ---------- AI-02 문제 ----------

MAX_STEM = 300
# 개념이 아니라 자료의 문구·제목·쪽 표시를 묻는 지문. 작은 단원에서 많이 나왔다 (eval/결과.md 11-06).
# 다시 만들면 되므로 애매한 표현도 넓게 거른다.
META_STEM = re.compile(
    r"(자료|슬라이드|목차|강의)(에서는|에서|에는|에|는|의|가)\s"
    r"|(라고|다고) (설명|적혀|제시|표현|소개|강조)"
    r"|제목|항목에는|항목의"
    r"|표현이 (나온|등장|쓰|있)"
    r"|\d+\s*/\s*\d+\s*(로|으로)?\s*표시"
)
MAX_EXPLANATION = 600
MIN_QUOTE = 10  # 정규화 후 글자 수. 너무 짧은 인용은 어디에나 있어 근거가 안 된다
MAX_SHORT_ANSWER = 20
MAX_ACCEPTED = 5
MAX_TAGS = 3
MAX_TAG = 20
SIMILAR = 0.75  # 같은 것을 말만 바꿔 물은 지문(0.75~0.89)을 거른다 (eval/결과.md 11-06)
_BANNED_CHOICE = re.compile(r"모두\s*(옳|맞|틀)|정답\s*(이\s*)?없|위의?\s*보기")
_NON_WORD = re.compile(r"[\W_]+")


def normalize(text: str) -> str:
    """근거 대조·중복 판정용. 공백과 문장부호를 지우고 소문자로 맞춘다."""
    return _NON_WORD.sub("", unicodedata.normalize("NFKC", text).lower())


def check_questions(
    drafts: list[QuestionBase],
    qtype: str,
    difficulty: int,
    pages: dict[int, str],
    exclude_stems: list[str],
    rng: random.Random | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """검증을 통과한 문제를 API 응답 형태로 바꾸고, 탈락 사유별 개수를 센다.

    pages는 단원에 속한 쪽만 담는다. exclude_stems는 이미 있는 문제 지문이다.
    """
    rng = rng or random.Random()
    norm_pages = {p: normalize(t) for p, t in pages.items()}
    norm_all = "".join(norm_pages.values())
    seen = [normalize(s) for s in exclude_stems]
    accepted, rejected = [], {"schema": 0, "evidence": 0, "duplicate": 0}

    for d in drafts:
        reason, question = _check_one(d, qtype, difficulty, norm_pages, norm_all, rng)
        if reason is None:
            stem = normalize(d.stem)
            if any(stem == s or difflib.SequenceMatcher(None, stem, s).ratio() >= SIMILAR for s in seen):
                reason = "duplicate"
            else:
                seen.append(stem)
                accepted.append(question)
                continue
        rejected[reason] += 1
    return accepted, rejected


def _check_one(d, qtype, difficulty, norm_pages, norm_all, rng) -> tuple[str | None, dict | None]:
    if not d.stem.strip() or len(d.stem) > MAX_STEM or META_STEM.search(d.stem):
        return "schema", None
    if not d.explanation.strip() or len(d.explanation) > MAX_EXPLANATION:
        return "schema", None
    if d.source_page not in norm_pages:
        return "schema", None
    page = _evidence_page(d.evidence_quote, d.source_page, norm_pages)
    if page is None:
        return "evidence", None

    question = {
        "type": qtype,
        "difficulty": difficulty,
        "stem": d.stem.strip(),
        "choices": [],
        "answer": "",
        "acceptedAnswers": [],
        "rubric": [],
        "explanation": d.explanation.strip(),
        "sourcePage": page,
        "evidenceQuote": d.evidence_quote.strip(),
        "conceptTags": [t.strip() for t in d.concept_tags if t.strip() and len(t.strip()) <= MAX_TAG][:MAX_TAGS],
    }

    match d:
        case OXDraft():
            question["answer"] = d.answer
        case MCDraft():
            options = [d.correct_choice.strip(), *(w.strip() for w in d.wrong_choices)]
            if len(options) != 4 or not all(options) or len({normalize(o) for o in options}) != 4:
                return "schema", None
            if any(_BANNED_CHOICE.search(o) for o in options):
                return "schema", None
            # LLM은 정답을 특정 위치에 몰아 두는 경향이 있어 섞는 일은 코드가 한다.
            question["choices"] = rng.sample(options, 4)
            question["answer"] = str(question["choices"].index(options[0]) + 1)
        case ShortDraft():
            answer = d.answer.strip()
            if not answer or len(answer) > MAX_SHORT_ANSWER:
                return "schema", None
            if normalize(answer) not in norm_all:  # 정답 용어가 자료에 없으면 자료 밖 문제다
                return "evidence", None
            question["answer"] = answer
            question["acceptedAnswers"] = _unique([answer, *(a.strip() for a in d.accepted_answers if len(a.strip()) <= 30)])[
                :MAX_ACCEPTED
            ]
        case EssayDraft():
            rubric = [r.strip() for r in d.rubric if r.strip()]
            if not 3 <= len(rubric) <= 5:
                return "schema", None
            question["rubric"] = rubric
        case _:
            return "schema", None
    return None, question


def _evidence_page(quote: str, source_page: int, norm_pages: dict[int, str]) -> int | None:
    """근거 문장이 있는 쪽. LLM이 쪽을 하나 틀리는 경우가 있어 앞뒤 쪽도 본다."""
    q = normalize(quote)
    if len(q) < MIN_QUOTE:
        return None
    for page in (source_page, source_page - 1, source_page + 1):
        if q in norm_pages.get(page, ""):
            return page
    return None


def _unique(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        key = normalize(item)
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return out

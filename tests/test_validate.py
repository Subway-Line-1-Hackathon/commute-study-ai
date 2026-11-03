import random

import pytest

from app.schemas import CategoryDraft, EssayDraft, MCDraft, OXDraft, ShortDraft
from app.validate import InvalidOutput, check_questions, fix_categories, normalize

# ---------- AI-01 ----------


def draft(start: int, end: int = 0, name: str = "", summary: str = "요약") -> CategoryDraft:
    return CategoryDraft(name=name or f"단원{start}", page_start=start, page_end=end, summary=summary)


def bounds(cats: list[dict]) -> list[tuple]:
    return [(c["order"], c["pageStart"], c["pageEnd"]) for c in cats]


def test_boundaries_follow_starts_only():
    cats = fix_categories([draft(1, 3), draft(4, 9), draft(8, 8)], [500] * 10)
    assert bounds(cats) == [(1, 1, 3), (2, 4, 7), (3, 8, 10)]


def test_unsorted_and_first_start_not_one():
    cats = fix_categories([draft(6), draft(3)], [500] * 8)
    assert bounds(cats) == [(1, 1, 5), (2, 6, 8)]
    assert cats[0]["name"] == "단원3"


def test_small_category_merges_into_previous():
    cats = fix_categories([draft(1), draft(3), draft(4)], [400, 400, 50, 400])
    assert bounds(cats) == [(1, 1, 3), (2, 4, 4)]
    assert cats[0]["name"] == "단원1"
    assert cats[0]["summary"] == "요약\n요약"


def test_small_first_category_merges_into_next():
    cats = fix_categories([draft(1, name="표지"), draft(2, name="스택")], [20, 800])
    assert bounds(cats) == [(1, 1, 2)]
    assert cats[0]["name"] == "스택"


def test_everything_small_becomes_one_category():
    assert bounds(fix_categories([draft(1), draft(2), draft(3)], [10, 10, 10])) == [(1, 1, 3)]


@pytest.mark.parametrize(
    "drafts",
    [
        [],
        [draft(1), draft(1)],
        [draft(0)],
        [draft(1), draft(11)],
        [draft(1, summary=" ")],
        [draft(1, summary="가" * 1501)],
        [draft(i) for i in range(1, 22)],
    ],
)
def test_invalid_category_output(drafts):
    with pytest.raises(InvalidOutput):
        fix_categories(drafts, [500] * 30 if len(drafts) > 20 else [500] * 10)


# ---------- AI-02 ----------

PAGES = {
    3: "스택은 나중에 넣은 데이터가 먼저 나오는 구조다. 푸시와 팝 연산을 쓴다.",
    4: "큐는 먼저 넣은 데이터가 먼저 나오는 구조다. 인큐와 디큐 연산을 쓴다.",
}
STACK = "스택은 나중에 넣은 데이터가 먼저 나오는 구조다."
QUEUE = "큐는 먼저 넣은 데이터가 먼저 나오는 구조다."


def base(**overrides) -> dict:
    fields = {
        "stem": "스택은 LIFO 구조다.",
        "explanation": "스택은 나중에 넣은 것이 먼저 나와요.",
        "source_page": 3,
        "evidence_quote": STACK,
        "concept_tags": ["스택"],
    }
    return {**fields, **overrides}


def check(drafts, qtype="OX", exclude=(), seed=0):
    return check_questions(drafts, qtype, 2, PAGES, list(exclude), random.Random(seed))


def test_ox_passes_with_api_shape():
    accepted, rejected = check([OXDraft(answer="O", **base())])
    assert rejected == {"schema": 0, "evidence": 0, "duplicate": 0}
    assert accepted == [
        {
            "type": "OX",
            "difficulty": 2,
            "stem": "스택은 LIFO 구조다.",
            "choices": [],
            "answer": "O",
            "acceptedAnswers": [],
            "rubric": [],
            "explanation": "스택은 나중에 넣은 것이 먼저 나와요.",
            "sourcePage": 3,
            "evidenceQuote": STACK,
            "conceptTags": ["스택"],
        }
    ]


def test_quote_matches_despite_spacing_and_punctuation():
    accepted, _ = check([OXDraft(answer="O", **base(evidence_quote="스택은  나중에 넣은 데이터가, 먼저 나오는 구조다"))])
    assert len(accepted) == 1


def test_quote_on_neighbor_page_fixes_source_page():
    accepted, _ = check([OXDraft(answer="X", **base(source_page=3, evidence_quote=QUEUE))])
    assert accepted[0]["sourcePage"] == 4


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"evidence_quote": "자료에 없는 문장이 근거라고 주장한다"}, "evidence"),
        ({"evidence_quote": "스택은"}, "evidence"),
        ({"source_page": 9}, "schema"),
        ({"stem": " "}, "schema"),
        ({"explanation": "가" * 601}, "schema"),
    ],
)
def test_rejections(overrides, reason):
    accepted, rejected = check([OXDraft(answer="O", **base(**overrides))])
    assert accepted == []
    assert rejected[reason] == 1


def test_concept_tags_are_trimmed():
    tags = ["스택", " ", "가" * 21, "LIFO", "자료구조", "넷째"]
    accepted, _ = check([OXDraft(answer="O", **base(concept_tags=tags))])
    assert accepted[0]["conceptTags"] == ["스택", "LIFO", "자료구조"]


def mc(**overrides) -> MCDraft:
    fields = {"correct_choice": "큐", "wrong_choices": ["스택", "트리", "그래프"]}
    return MCDraft(**{**base(stem="다음 중 FIFO 구조는?", evidence_quote=QUEUE, source_page=4), **fields, **overrides})


def test_mc_shuffle_points_to_correct_choice_and_spreads_positions():
    positions = set()
    for seed in range(100):
        q = check([mc()], "MC", seed=seed)[0][0]
        assert q["choices"][int(q["answer"]) - 1] == "큐"
        positions.add(q["answer"])
    assert positions == {"1", "2", "3", "4"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"wrong_choices": ["스택", "트리"]},
        {"wrong_choices": ["스택", "트리", " 큐 "]},
        {"wrong_choices": ["스택", "트리", "모두 옳다"]},
        {"wrong_choices": ["스택", "트리", "정답 없음"]},
        {"correct_choice": ""},
    ],
)
def test_mc_invalid_choices(overrides):
    assert check([mc(**overrides)], "MC")[1]["schema"] == 1


def short(**overrides) -> ShortDraft:
    fields = {"answer": "스택", "accepted_answers": ["Stack", "스 택", "stack", "가" * 31]}
    return ShortDraft(**{**base(stem="나중에 넣은 것이 먼저 나오는 구조는?"), **fields, **overrides})


def test_short_accepted_answers_deduplicated():
    q = check([short()], "SHORT")[0][0]
    assert q["answer"] == "스택"
    assert q["acceptedAnswers"] == ["스택", "Stack"]


def test_short_answer_must_appear_in_material():
    assert check([short(answer="해시 테이블")], "SHORT")[1]["evidence"] == 1


def test_short_answer_too_long():
    assert check([short(answer="가" * 21)], "SHORT")[1]["schema"] == 1


@pytest.mark.parametrize(("rubric", "ok"), [(["a", "b"], False), (["a", "b", "c", "d"], True), (["a"] * 6, False)])
def test_essay_rubric_size(rubric, ok):
    accepted, _ = check([EssayDraft(rubric=rubric, **base(stem="스택을 설명하시오."))], "ESSAY")
    assert bool(accepted) is ok


def test_duplicates_against_existing_and_within_batch():
    drafts = [
        OXDraft(answer="O", **base(stem="스택은 LIFO 구조다")),
        OXDraft(answer="O", **base(stem="큐는 FIFO 구조다.")),
        OXDraft(answer="O", **base(stem="큐는 FIFO 구조이다.")),
    ]
    accepted, rejected = check(drafts, exclude=["스택은 LIFO 구조다."])
    assert [q["stem"] for q in accepted] == ["큐는 FIFO 구조다."]
    assert rejected["duplicate"] == 2


def test_normalize():
    assert normalize("Ｑueue, 큐!  (FIFO)") == "queue큐fifo"

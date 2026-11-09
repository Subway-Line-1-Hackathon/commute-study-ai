import random

import pytest

from app.schemas import CategoryDraft, EssayDraft, MCDraft, OXDraft, Point, QuestionSpec, ShortDraft
from app.validate import InvalidOutput, assign_points, capacity, check_points, check_questions, fix_categories, normalize

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


def test_small_category_merges_into_smaller_neighbor():
    cats = fix_categories([draft(1), draft(2), draft(3)], [3000, 50, 1000])
    assert bounds(cats) == [(1, 1, 1), (2, 2, 3)]
    assert cats[1]["name"] == "단원3"  # 이름은 큰 쪽
    assert cats[1]["summary"] == "요약\n요약"


def test_small_categories_pair_up_instead_of_chaining():
    # 앞 단원에만 합치면 2~5쪽이 모두 한 단원이 된다
    cats = fix_categories([draft(i) for i in range(1, 7)], [2000, 600, 600, 600, 600, 2000])
    assert bounds(cats) == [(1, 1, 1), (2, 2, 3), (3, 4, 5), (4, 6, 6)]


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


def point(statement="스택은 LIFO 구조다.", page=3, quote=STACK, importance="core", types=("OX", "MC", "SHORT"), concept="") -> Point:
    return Point(
        statement=statement,
        concept=concept or statement,
        source_page=page,
        evidence_quote=quote,
        importance=importance,
        types=list(types),
    )


POINTS = {1: point(), 2: point("큐는 FIFO 구조다.", 4, QUEUE)}


def base(**overrides) -> dict:
    fields = {
        "stem": "스택은 LIFO 구조다.",
        "explanation": "스택은 나중에 넣은 것이 먼저 나와요.",
        "point": 1,
        "concept_tags": ["스택"],
    }
    return {**fields, **overrides}


def check(drafts, qtype="OX", exclude=(), seed=0, pages=None):
    return check_questions(drafts, qtype, 2, POINTS, pages or PAGES, list(exclude), random.Random(seed))


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
            "point": 1,
        }
    ]


def test_point_quote_matches_despite_spacing_and_punctuation():
    assert len(check_points([point(quote="스택은  나중에 넣은 데이터가, 먼저 나오는 구조다")], PAGES)) == 1


def test_point_quote_on_neighbor_page_fixes_source_page():
    assert check_points([point(page=3, quote=QUEUE)], PAGES)[0].source_page == 4


@pytest.mark.parametrize(
    "bad",
    [
        point(quote="자료에 없는 문장이 근거라고 주장한다"),
        point(quote="스택은"),
        point(types=()),
        point(statement="자료에서는 스택을 LIFO라고 설명한다."),
        point(statement="스택은 LIFO 구조이다."),  # 첫 포인트와 거의 같다
    ],
)
def test_bad_points_are_dropped(bad):
    assert check_points([point(), bad], PAGES) == [point()]


def test_capacity_counts_core_types_and_one_per_minor():
    points = [point("가"), point("나", types=("OX",)), point("다", importance="minor"), point("라", importance="minor")]
    assert capacity(points) == 2 + 1 + 1 + 1
    assert capacity([point(f"사실 {i}") for i in range(30)]) == 40


def test_one_concept_gets_at_most_four_questions():
    points = [point(f"DOCTYPE 사실 {i}", concept="DOCTYPE 선언") for i in range(5)] + [point("p 태그")]
    assert capacity(points) == 4 + 2


def spec(qtype, count, difficulty=2):
    return QuestionSpec(type=qtype, difficulty=difficulty, count=count)


def test_assign_spreads_types_and_never_repeats_a_point_type():
    points = [point(f"사실 {i}") for i in range(4)] + [point("보조", importance="minor", types=("OX",))]
    out = assign_points(points, [spec("OX", 5), spec("MC", 5), spec("SHORT", 5)], budget=40)
    assert sum(map(len, out)) == 9  # core 4개 × 2문제 + minor 1개
    assert [len(o) for o in out] == [3, 3, 3]
    for nums in out:
        assert len(nums) == len(set(nums))
    assert 5 in out[0]


def test_assign_uses_unused_points_first_and_respects_budget():
    points = [point(f"사실 {i}") for i in range(6)]
    out = assign_points(points, [spec("OX", 4), spec("MC", 4)], budget=6)
    assert sum(map(len, out)) == 6
    assert sorted(out[0] + out[1]) == [1, 2, 3, 4, 5, 6]


def test_assign_keeps_request_ratio_when_capacity_is_short():
    points = [point(f"사실 {i}") for i in range(6)]
    out = assign_points(points, [spec("OX", 14), spec("MC", 13), spec("SHORT", 13)], budget=9)
    assert [len(o) for o in out] == [3, 3, 3]


def test_assign_fills_missing_types_with_other_types():
    points = [point(f"사실 {i}", types=("OX", "MC")) for i in range(3)]
    out = assign_points(points, [spec("OX", 1), spec("SHORT", 2)], budget=3)
    assert out == [[1, 2, 3], []]


def test_capacity_depends_on_requested_types():
    points = [point(f"사실 {i}", types=("OX", "MC")) for i in range(3)]
    assert capacity(points, ["SHORT"]) == 0
    assert capacity(points, ["OX", "SHORT"]) == 3
    assert capacity(points) == 6


def test_assign_essay_only_from_core_points():
    points = [point("보조", importance="minor"), point("핵심")]
    assert assign_points(points, [spec("ESSAY", 2)], budget=5) == [[2]]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"point": 9}, "schema"),
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
    return MCDraft(**{**base(stem="다음 중 FIFO 구조는?", point=2), **fields, **overrides})


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


@pytest.mark.parametrize(
    ("draft", "qtype"),
    [
        (lambda: short(stem="Config가 OPENAI_API_KEY 값을 읽는 환경 변수 이름은?", answer="OPENAI_API_KEY"), "SHORT"),
        (lambda: mc(stem="@app.route(\"/about\")로 등록한 함수의 URL은?", correct_choice="/about"), "MC"),
    ],
)
def test_answer_written_in_the_stem_is_rejected(draft, qtype):
    assert check([draft()], qtype)[1]["schema"] == 1


def test_tag_answer_is_not_leaked_by_the_language_name():
    accepted, _ = check([short(stem="다른 모든 HTML 요소를 담는 컨테이너 태그는?", answer="<html>")], "SHORT", pages={3: "<html> 스택은"})
    assert len(accepted) == 1


def test_short_answers_may_appear_in_stem_by_chance():
    accepted, _ = check([short(stem="스택에서 나중에 넣은 것이 먼저 나오는 구조는?", answer="스택")], "SHORT")
    assert len(accepted) == 1


def test_mc_choices_differing_only_by_symbols_are_distinct():
    accepted, _ = check([mc(correct_choice="plain", wrong_choices=["<<plain>>", "plain***", "FIXED"])], "MC")
    assert len(accepted) == 1


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
        OXDraft(answer="O", **base(stem="큐는 FIFO 구조다.", point=2)),
        OXDraft(answer="O", **base(stem="큐는 FIFO 구조이다.")),
    ]
    accepted, rejected = check(drafts, exclude=["스택은 LIFO 구조다."])
    assert [q["stem"] for q in accepted] == ["큐는 FIFO 구조다."]
    assert rejected["duplicate"] == 2


def test_one_question_per_point():
    drafts = [OXDraft(answer="O", **base()), OXDraft(answer="X", **base(stem="푸시와 팝은 스택의 연산이 아니다."))]
    accepted, rejected = check(drafts)
    assert len(accepted) == 1
    assert rejected["duplicate"] == 1


def test_normalize():
    assert normalize("Ｑueue, 큐!  (FIFO)") == "queue큐fifo"


@pytest.mark.parametrize(
    "stem",
    [
        "자료에서는 모습이 달라도 의미는 같을 수 있다고 설명한다.",
        "Pointer to Array의 첫 번째 항목은 4/4로 표시되어 있다.",
        "Pointer to Array Example 항목의 제목에는 again이 포함되어 있다.",
        "Pointers and Arrays 항목에는 NEXT ‘ELEMENT’라는 표현이 나온다.",
        "자료의 목차에서 Function Pointer와 함께 제시된 항목은 무엇인가?",
        "자료는 배열을 객체가 아니라고 설명한다.",
        "슬라이드 제목에 again이 포함되어 있다.",
    ],
)
def test_questions_about_the_material_wording_are_rejected(stem):
    accepted, rejected = check([OXDraft(answer="O", **base(stem=stem))])
    assert accepted == []
    assert rejected["schema"] == 1


@pytest.mark.parametrize(
    "stem",
    [
        "자료구조 중 스택은 LIFO 구조다.",
        "C++의 raw array는 객체가 아니어서 size() 같은 멤버 함수가 없다.",
        "int a[4]에서 a + 1은 다음 바이트가 아니라 다음 요소를 가리킨다.",
        "서로 다른 수준의 제목을 정의하는 HTML 태그는 h1부터 h6까지다.",
    ],
)
def test_concept_questions_pass_the_wording_filter(stem):
    accepted, _ = check([OXDraft(answer="O", **base(stem=stem))])
    assert len(accepted) == 1

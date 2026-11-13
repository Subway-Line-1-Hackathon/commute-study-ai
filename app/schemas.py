"""요청 모델과 LLM 출력 모델. 요청 필드와 값은 docs/API_명세.md를 따른다."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from pydantic.alias_generators import to_camel

FileType = Literal["PDF", "PPT", "PNG", "JPG"]
Kind = Literal["LECTURE", "PAST_EXAM"]
QuestionType = Literal["OX", "MC", "SHORT", "ESSAY"]
Difficulty = Literal[1, 2, 3]
Mode = Literal["CURRICULUM", "REVIEW"]


class Model(BaseModel):
    # JSON은 camelCase, 코드는 snake_case. 모르는 필드는 백엔드 오타일 수 있어 거부한다.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Page(Model):
    page: int = Field(ge=1)
    text: str


class Category(Model):
    category_id: int
    name: str
    pages: list[Page] = Field(min_length=1)


class ProcessMaterialRequest(Model):
    material_id: int
    file_type: FileType
    kind: Kind
    file_name: str
    file_urls: list[HttpUrl] = Field(min_length=1, max_length=20)
    note_urls: list[HttpUrl] = Field(default=[], max_length=20)  # 필기 사진(PNG·JPG). 강의자료 다음 쪽 번호를 받는다

    @model_validator(mode="after")
    def one_url_unless_image(self):
        if self.file_type in ("PDF", "PPT") and len(self.file_urls) != 1:
            raise ValueError("PDF·PPT는 fileUrls가 1개여야 한다")
        return self


class QuestionSpec(Model):
    type: QuestionType
    difficulty: Difficulty
    count: int = Field(ge=1)


class GenerateQuestionsRequest(Model):
    category: Category
    requests: list[QuestionSpec] = Field(min_length=1)
    exclude_stems: list[str] = []

    @model_validator(mode="after")
    def at_most_40(self):
        if sum(r.count for r in self.requests) > 40:
            raise ValueError("count 합계는 40 이하여야 한다")
        return self


class JudgeShortRequest(Model):
    stem: str
    accepted_answers: list[str] = Field(min_length=1)
    explanation: str
    user_answer: str


class GradeEssayRequest(Model):
    stem: str
    rubric: list[str] = Field(min_length=1)
    explanation: str
    user_answer: str


class TutorQuestion(Model):
    stem: str
    choices: list[str] = []
    answer: str
    explanation: str
    user_answer: str = ""


class Turn(Model):
    role: Literal["user", "assistant"]
    content: str


class TutorAskRequest(Model):
    category: Category
    question: TutorQuestion
    history: list[Turn] = Field(default=[], max_length=6)
    message: str = Field(min_length=1)


class WrongItem(Model):
    stem: str
    concept_tags: list[str] = []


class FeedbackRequest(Model):
    session_id: int
    mode: Mode
    total: int = Field(ge=0)
    answered: int = Field(ge=0)
    correct: int = Field(ge=0)
    correct_concepts: list[str] = []
    wrong_items: list[WrongItem] = []


# LLM 출력 모델. LLM이 채우는 필드만 둔다. 유형·난이도·정답 번호는 코드가 채운다.
# 구조화 출력은 모든 필드가 필수라 기본값을 두지 않는다.


class CategoryDraft(BaseModel):
    name: str
    page_start: int
    page_end: int
    summary: str


class IssueDraft(BaseModel):
    level: Literal["wrong", "nuance"]  # 모델이 스스로 가른다. 학생에게는 wrong만 알린다
    page: int
    quote: str  # 틀린 부분 원문. 코드가 자료에 있는지 확인한다
    problem: str
    correction: str


class IssuesOut(BaseModel):
    issues: list[IssueDraft]


class NoteDraft(BaseModel):
    page: int  # 필기 쪽 번호
    related_pages: list[int]  # 관련 있는 강의자료 쪽 번호. 코드가 이 쪽들이 속한 단원마다 필기를 붙인다


class CategoriesOut(BaseModel):
    categories: list[CategoryDraft]
    notes: list[NoteDraft]


class Point(BaseModel):
    """출제 포인트. 학생이 확인해야 할 사실 하나와 그 근거."""

    statement: str
    concept: str  # 같은 개념의 포인트끼리 같은 이름. 한 개념에 문제가 몰리지 않게 센다
    source_page: int
    evidence_quote: str
    importance: Literal["core", "minor"]
    types: list[Literal["OX", "MC", "SHORT"]]


class PlanOut(BaseModel):
    points: list[Point]


class QuestionBase(BaseModel):
    point: int  # 출제 포인트 번호. 쪽과 근거는 코드가 포인트에서 붙인다
    stem: str
    explanation: str
    concept_tags: list[str]


class OXDraft(QuestionBase):
    answer: Literal["O", "X"]


class MCDraft(QuestionBase):
    correct_choice: str
    wrong_choices: list[str]


class ShortDraft(QuestionBase):
    answer: str
    accepted_answers: list[str]


class EssayDraft(QuestionBase):
    rubric: list[str]


class OXOut(BaseModel):
    questions: list[OXDraft]


class MCOut(BaseModel):
    questions: list[MCDraft]


class ShortOut(BaseModel):
    questions: list[ShortDraft]


class EssayOut(BaseModel):
    questions: list[EssayDraft]


QUESTION_OUTPUTS: dict[str, type[BaseModel]] = {"OX": OXOut, "MC": MCOut, "SHORT": ShortOut, "ESSAY": EssayOut}


class OcrText(BaseModel):
    text: str


class JudgeOut(BaseModel):
    correct: bool
    reason: str


class FeedbackOut(BaseModel):
    feedback: str

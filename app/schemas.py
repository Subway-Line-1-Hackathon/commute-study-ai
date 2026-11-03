"""요청 모델과 LLM 출력 모델. 요청 필드와 값은 docs/API_명세.md를 따른다."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from pydantic.alias_generators import to_camel

FileType = Literal["PDF", "PPT", "PNG"]
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

    @model_validator(mode="after")
    def one_url_unless_png(self):
        if self.file_type != "PNG" and len(self.file_urls) != 1:
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


class CategoriesOut(BaseModel):
    categories: list[CategoryDraft]


class QuestionBase(BaseModel):
    stem: str
    explanation: str
    source_page: int
    evidence_quote: str
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

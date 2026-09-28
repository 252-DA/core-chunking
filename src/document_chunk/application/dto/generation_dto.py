from dataclasses import dataclass

from pydantic import BaseModel

from document_chunk.domain.ports.metadata_store import IngestionStatus


class DocumentStatusResponse(BaseModel):
    document_id: str
    status: IngestionStatus
    error_msg: str | None = None
    storage_key: str | None = None


class LessonCardItem(BaseModel):
    card_id: str
    chunk_id: str
    heading_path: list[str]
    title: str
    bullets: list[str]
    key_insight: str | None = None
    card_index: int


class CardSection(BaseModel):
    heading_path: list[str]
    cards: list[LessonCardItem]


class CardsResponse(BaseModel):
    document_id: str
    sections: list[CardSection]
    total_cards: int


class QuizQuestionItem(BaseModel):
    question_id: str
    chunk_id: str
    question: str
    choices: list[str]
    correct_index: int
    explanation: str | None = None
    difficulty: str = "medium"


class QuizResponse(BaseModel):
    document_id: str
    questions: list[QuizQuestionItem]
    total_questions: int


@dataclass
class GenerateCurriculumQuizRequest:
    course_id: str
    target_kind: str
    target_code: str
    style: str = "quiz"
    bloom_level: str | None = None
    count: int = 5
    source_document_ids: list[str] | None = None


@dataclass
class GenerateCurriculumQuizResponse:
    course_id: str
    lo_id: str
    quiz_count: int
    question_ids: list[str]

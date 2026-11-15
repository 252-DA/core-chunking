from dataclasses import dataclass


@dataclass
class GenerateCurriculumQuizRequest:
    course_id: str
    target_kind: str
    target_code: str
    style: str = "quiz"
    bloom_level: str | None = None
    count: int = 5
    source_document_ids: list[str] | None = None
    # Một kiểu câu cho cả mẻ; xem core-api quiz/question-types.ts.
    question_type: str = "MCQ_SINGLE"


@dataclass
class GenerateCurriculumQuizResponse:
    course_id: str
    lo_id: str
    quiz_count: int
    question_ids: list[str]

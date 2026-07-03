from dataclasses import dataclass, field


@dataclass(frozen=True)
class Course:
    course_id: str
    code: str
    title_vi: str
    title_en: str | None = None
    credits: int | None = None
    semester: str | None = None


@dataclass(frozen=True)
class Chapter:
    chapter_id: str
    code: str
    title: str
    order_index: int = 0


@dataclass(frozen=True)
class Topic:
    topic_id: str
    chapter_code: str
    title: str
    order_index: int = 0


@dataclass(frozen=True)
class LearningOutcome:
    lo_id: str          # "CO3115:L.O.3.5"
    code: str           # "L.O.3.5"
    parent_code: str | None
    statement_vi: str
    statement_en: str | None = None
    bloom_level: str | None = None
    cdio_level: int | None = None


@dataclass(frozen=True)
class Assessment:
    assessment_id: str
    code: str           # "A.O.4"
    name_vi: str
    name_en: str | None = None
    category: str = "quiz"   # quiz | midterm | final | project | group_quiz
    weight: float | None = None


@dataclass(frozen=True)
class Curriculum:
    course: Course
    chapters: tuple[Chapter, ...]
    learning_outcomes: tuple[LearningOutcome, ...]
    assessments: tuple[Assessment, ...]
    lo_assessment_links: tuple[tuple[str, str], ...]  # (lo_id, assessment_id)
    source_document_id: str | None = None
    extraction_confidence: float = 0.0

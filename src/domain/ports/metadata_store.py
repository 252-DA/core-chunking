from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from src.domain.entities.document import Document, DocumentType
from src.shared.result import Result


class IngestionStatus(str, Enum):
    QUEUED = "QUEUED"
    PARSING = "PARSING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    UPSERTING = "UPSERTING"
    DONE = "DONE"
    ENRICHING = "ENRICHING"
    ENRICHED = "ENRICHED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class DocumentFilter:
    """Filter params cho metadata queries."""
    doc_types: tuple[DocumentType, ...] = ()
    language: str | None = None
    uploaded_after: datetime | None = None
    uploaded_before: datetime | None = None


@dataclass(frozen=True)
class StoredChunkMetadata:
    chunk_id: str
    document_id: str
    chunk_index: int
    heading_path: tuple[str, ...] = ()
    heading_level: int = 0
    page_number: int | None = None
    content_length: int = 0
    language: str | None = None
    content_text: str | None = None
    embedding_input: str | None = None


@dataclass(frozen=True)
class StoredDocumentContext:
    document_id: str
    course_id: str | None = None
    owner_id: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class StoredConcept:
    concept_id: str
    name: str
    canonical_name: str
    slug: str
    category: str = "other"
    language: str | None = None
    domain: str | None = None


@dataclass(frozen=True)
class StoredChunkConcept:
    chunk_id: str
    concept_id: str
    confidence: float = 1.0
    source: str = "heading"


@dataclass(frozen=True)
class StoredLessonCard:
    card_id: str
    document_id: str
    primary_chunk_id: str
    source_chunk_ids: tuple[str, ...] = ()
    heading_path: tuple[str, ...] = ()
    title: str = ""
    bullets: tuple[str, ...] = ()
    key_insight: str | None = None
    card_index: int = 0
    model_id: str | None = None


@dataclass(frozen=True)
class StoredQuizItem:
    question_id: str
    document_id: str
    primary_chunk_id: str
    source_chunk_ids: tuple[str, ...] = ()
    heading_path: tuple[str, ...] = ()
    question: str = ""
    choices: tuple[str, ...] = ()
    correct_index: int = 0
    explanation: str | None = None
    difficulty: str = "medium"
    question_index: int = 0
    model_id: str | None = None


@dataclass(frozen=True)
class OutboxEvent:
    id: str
    event_type: str
    aggregate_id: str
    payload: dict
    status: str = "PENDING"
    attempts: int = 0
    error_msg: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class StoredCourse:
    course_id: str
    code: str
    title_vi: str
    title_en: str | None = None
    credits: int | None = None
    semester: str | None = None
    source_document_id: str | None = None
    extraction_confidence: float = 0.0


@dataclass(frozen=True)
class StoredChapter:
    chapter_id: str
    course_id: str
    code: str
    title: str
    order_index: int = 0


@dataclass(frozen=True)
class StoredLearningOutcome:
    lo_id: str
    course_id: str
    code: str
    parent_code: str | None
    statement_vi: str
    statement_en: str | None = None
    bloom_level: str | None = None
    cdio_level: int | None = None


@dataclass(frozen=True)
class StoredAssessment:
    assessment_id: str
    course_id: str
    code: str
    name_vi: str
    name_en: str | None = None
    category: str = "quiz"
    weight: float | None = None


@dataclass(frozen=True)
class StoredChunkLOMapping:
    chunk_id: str
    lo_id: str
    confidence: float
    source: str


class IMetadataStore(ABC):
    """
    Port: SQL metadata + ingestion status + outbox events.
    SQL là source of truth, Qdrant/Neo4j là derived stores.
    """

    @abstractmethod
    def upsert_document(
        self,
        document: Document,
        metadata: dict[str, str],
        status: IngestionStatus = IngestionStatus.QUEUED,
    ) -> Result[None, Exception]:
        """Insert/update document metadata."""
        ...

    @abstractmethod
    def update_document_status(
        self,
        document_id: str,
        status: IngestionStatus,
        error_msg: str | None = None,
    ) -> Result[None, Exception]:
        """Update status document theo state machine."""
        ...

    @abstractmethod
    def upsert_chunks(
        self,
        chunks: list[StoredChunkMetadata],
    ) -> Result[None, Exception]:
        """Bulk upsert chunk metadata."""
        ...

    @abstractmethod
    def upsert_chunks_with_outbox(
        self,
        chunks: list[StoredChunkMetadata],
        event_type: str,
        aggregate_id: str,
        payload: dict,
    ) -> Result[str, Exception]:
        """Bulk upsert chunk metadata and append one outbox event in the same transaction."""
        ...

    @abstractmethod
    def get_document_context(
        self,
        document_id: str,
    ) -> Result[StoredDocumentContext | None, Exception]:
        """Lấy context document phục vụ enrichment."""
        ...

    @abstractmethod
    def list_chunks(
        self,
        document_id: str,
    ) -> Result[list[StoredChunkMetadata], Exception]:
        """Lấy chunks đã persist theo document_id, sort theo chunk_index."""
        ...

    @abstractmethod
    def upsert_concepts(
        self,
        concepts: list[StoredConcept],
    ) -> Result[None, Exception]:
        """Bulk upsert concepts."""
        ...

    @abstractmethod
    def upsert_chunk_concepts(
        self,
        chunk_concepts: list[StoredChunkConcept],
    ) -> Result[None, Exception]:
        """Bulk upsert chunk-concept mentions."""
        ...

    @abstractmethod
    def persist_enrichment_batch(
        self,
        document_id: str,
        lesson_cards: list[StoredLessonCard],
        quiz_items: list[StoredQuizItem],
        concepts: list[StoredConcept],
        chunk_concepts: list[StoredChunkConcept],
        outbox_event_type: str | None = None,
        outbox_payload: dict | None = None,
    ) -> Result[str | None, Exception]:
        """
        Replace generated content for one document and optionally append one outbox event
        in the same transaction. Returns the appended event_id, or None when no outbox
        event is written.
        """
        ...

    @abstractmethod
    def list_lesson_cards(
        self,
        document_id: str,
    ) -> Result[list[StoredLessonCard], Exception]:
        """List generated lesson cards for one document in stable display order."""
        ...

    @abstractmethod
    def list_quiz_items(
        self,
        document_id: str,
    ) -> Result[list[StoredQuizItem], Exception]:
        """List generated quiz items for one document in stable display order."""
        ...

    @abstractmethod
    def append_outbox_event(
        self,
        event_type: str,
        aggregate_id: str,
        payload: dict,
    ) -> Result[str, Exception]:
        """Append event vào outbox, trả về event_id."""
        ...

    @abstractmethod
    def fetch_pending_outbox(self, limit: int = 100) -> Result[list[OutboxEvent], Exception]:
        """Lấy events PENDING theo thứ tự created_at."""
        ...

    @abstractmethod
    def mark_outbox_done(self, event_id: str) -> Result[None, Exception]:
        ...

    @abstractmethod
    def mark_outbox_failed(self, event_id: str, error_msg: str) -> Result[None, Exception]:
        ...

    @abstractmethod
    def get(self, document_id: str) -> Result[Document | None, Exception]:
        """Lấy document theo id."""
        ...

    @abstractmethod
    def list(
        self,
        filters: DocumentFilter | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Result[list[Document], Exception]:
        """List documents với optional filter."""
        ...

    @abstractmethod
    def delete(self, document_id: str) -> Result[None, Exception]:
        """Xóa document metadata/chunks và append cleanup outbox event trong cùng transaction."""
        ...

    @abstractmethod
    def get_document_status(
        self, document_id: str
    ) -> Result[tuple[IngestionStatus, str | None, str | None] | None, Exception]:
        """
        Trả về (status, error_msg, storage_key) của document, hoặc None nếu không tìm thấy.
        error_msg chỉ có giá trị khi status == ERROR.
        """
        ...

    # ------------------------------------------------------------------
    # Curriculum methods
    # ------------------------------------------------------------------

    @abstractmethod
    def upsert_curriculum(
        self,
        course: "StoredCourse",
        chapters: "list[StoredChapter]",
        learning_outcomes: "list[StoredLearningOutcome]",
        assessments: "list[StoredAssessment]",
        lo_assessment_links: "list[tuple[str, str]]",
    ) -> Result[None, Exception]:
        """Insert/update toàn bộ curriculum cho một course trong một transaction."""
        ...

    @abstractmethod
    def get_curriculum(
        self, course_id: str
    ) -> Result[
        "tuple[StoredCourse, list[StoredChapter], list[StoredLearningOutcome], list[StoredAssessment]] | None",
        Exception,
    ]:
        """Lấy curriculum đầy đủ theo course_id. None nếu chưa có."""
        ...

    @abstractmethod
    def list_los_by_chapter(
        self, course_id: str, chapter_code: str
    ) -> Result["list[StoredLearningOutcome]", Exception]:
        """Lấy LOs thuộc chapter (L.O.N.*) theo course_id + chapter_code."""
        ...

    @abstractmethod
    def list_los_by_assessment(
        self, course_id: str, assessment_code: str
    ) -> Result["list[StoredLearningOutcome]", Exception]:
        """Lấy LOs được evaluate bởi assessment_code."""
        ...

    @abstractmethod
    def upsert_chunk_lo_mappings(
        self,
        mappings: "list[StoredChunkLOMapping]",
    ) -> Result[None, Exception]:
        """Bulk upsert chunk→LO mapping, ON CONFLICT update confidence + source."""
        ...

    @abstractmethod
    def list_chunks_for_lo(
        self, lo_id: str
    ) -> Result["list[StoredChunkMetadata]", Exception]:
        """Lấy chunks đã map tới lo_id, sort theo confidence DESC."""
        ...

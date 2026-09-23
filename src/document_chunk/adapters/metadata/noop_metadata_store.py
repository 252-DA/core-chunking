from __future__ import annotations

from document_chunk.domain.entities.document import Document
from document_chunk.domain.outbox_events import OutboxEventType
from document_chunk.domain.ports.metadata_store import (
    DocumentFilter,
    DocumentSummary,
    IMetadataStore,
    IngestionStatus,
    StoredAssessment,
    StoredChapter,
    StoredChunkConcept,
    StoredChunkLOMapping,
    StoredChunkMetadata,
    StoredConcept,
    StoredCourse,
    StoredDocumentContext,
    StoredLearningOutcome,
    StoredLessonCard,
    StoredQuizItem,
)
from document_chunk.shared.result import Ok, Result


class NoopMetadataStore(IMetadataStore):
    """
    No-op metadata store.
    Dùng khi SQL chưa bật để không làm vỡ pipeline hiện tại.
    """

    def upsert_document(
        self,
        document: Document,
        metadata: dict[str, str],
        status: IngestionStatus = IngestionStatus.QUEUED,
    ) -> Result[None, Exception]:
        return Ok(None)

    def update_document_status(
        self,
        document_id: str,
        status: IngestionStatus,
        error_msg: str | None = None,
    ) -> Result[None, Exception]:
        return Ok(None)

    def upsert_chunks(self, chunks: list[StoredChunkMetadata]) -> Result[None, Exception]:
        return Ok(None)

    def upsert_chunks_with_outbox(
        self,
        chunks: list[StoredChunkMetadata],
        event_type: OutboxEventType,
        aggregate_id: str,
        payload: dict,
    ) -> Result[str, Exception]:
        return Ok("noop-event")

    def get_document_context(
        self,
        document_id: str,
    ) -> Result[StoredDocumentContext | None, Exception]:
        return Ok(None)

    def list_chunks(self, document_id: str) -> Result[list[StoredChunkMetadata], Exception]:
        return Ok([])

    def upsert_concepts(self, concepts: list[StoredConcept]) -> Result[None, Exception]:
        return Ok(None)

    def upsert_chunk_concepts(
        self,
        chunk_concepts: list[StoredChunkConcept],
    ) -> Result[None, Exception]:
        return Ok(None)

    def persist_enrichment_batch(
        self,
        document_id: str,
        lesson_cards: list[StoredLessonCard],
        quiz_items: list[StoredQuizItem],
        concepts: list[StoredConcept],
        chunk_concepts: list[StoredChunkConcept],
        outbox_event_type: OutboxEventType | None = None,
        outbox_payload: dict | None = None,
    ) -> Result[str | None, Exception]:
        if outbox_event_type is None:
            return Ok(None)
        return Ok("noop-event")

    def persist_curriculum_quiz_items(
        self,
        lo_id: str,
        bloom_level: str | int | None,
        quiz_items: list[StoredQuizItem],
    ) -> Result[None, Exception]:
        return Ok(None)

    def list_lesson_cards(self, document_id: str) -> Result[list[StoredLessonCard], Exception]:
        return Ok([])

    def list_quiz_items(self, document_id: str) -> Result[list[StoredQuizItem], Exception]:
        return Ok([])

    def append_outbox_event(
        self,
        event_type: OutboxEventType,
        aggregate_id: str,
        payload: dict,
    ) -> Result[str, Exception]:
        return Ok("noop-event")

    def get(self, document_id: str) -> Result[Document | None, Exception]:
        return Ok(None)

    def list(
        self,
        filters: DocumentFilter | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Result[list[Document], Exception]:
        return Ok([])

    def delete(self, document_id: str) -> Result[None, Exception]:
        return Ok(None)

    def get_document_status(
        self, document_id: str
    ) -> Result[tuple[IngestionStatus, str | None, str | None] | None, Exception]:
        return Ok(None)

    def upsert_curriculum(
        self,
        course: StoredCourse,
        chapters: list[StoredChapter],
        learning_outcomes: list[StoredLearningOutcome],
        assessments: list[StoredAssessment],
        lo_assessment_links: list[tuple[str, str]],
    ) -> Result[None, Exception]:
        return Ok(None)

    def get_curriculum(
        self, course_id: str
    ) -> Result[
        tuple[StoredCourse, list[StoredChapter], list[StoredLearningOutcome], list[StoredAssessment]] | None,
        Exception,
    ]:
        return Ok(None)

    def list_los_by_chapter(
        self, course_id: str, chapter_code: str
    ) -> Result[list[StoredLearningOutcome], Exception]:
        return Ok([])

    def list_los_by_assessment(
        self, course_id: str, assessment_code: str
    ) -> Result[list[StoredLearningOutcome], Exception]:
        return Ok([])

    def upsert_chunk_lo_mappings(
        self,
        mappings: list[StoredChunkLOMapping],
    ) -> Result[None, Exception]:
        return Ok(None)

    def list_chunks_for_lo(
        self, lo_id: str
    ) -> Result[list[StoredChunkMetadata], Exception]:
        return Ok([])

    def update_content_generation_request(
        self,
        request_id: str,
        status: str,
        generated_count: int | None = None,
        last_error: str | None = None,
    ) -> Result[None, Exception]:
        return Ok(None)

    def record_llm_usage(
        self,
        *,
        provider: str,
        model: str,
        use_case: str,
        status: str,
        course_id: str | None = None,
        user_id: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0,
        latency_ms: int | None = None,
        trace_id: str | None = None,
    ) -> Result[None, Exception]:
        return Ok(None)

    # ------------------------------------------------------------------
    # LMS / web admin methods
    # ------------------------------------------------------------------

    def list_documents(
        self,
        course_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Result[list[DocumentSummary], Exception]:
        return Ok([])

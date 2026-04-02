from src.domain.entities.document import Document
from src.domain.ports.metadata_store import (
    DocumentFilter,
    IMetadataStore,
    IngestionStatus,
    OutboxEvent,
    StoredChunkConcept,
    StoredChunkMetadata,
    StoredConcept,
    StoredDocumentContext,
)
from src.shared.result import Ok, Result


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
        event_type: str,
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

    def append_outbox_event(
        self,
        event_type: str,
        aggregate_id: str,
        payload: dict,
    ) -> Result[str, Exception]:
        return Ok("noop-event")

    def fetch_pending_outbox(self, limit: int = 100) -> Result[list[OutboxEvent], Exception]:
        return Ok([])

    def mark_outbox_done(self, event_id: str) -> Result[None, Exception]:
        return Ok(None)

    def mark_outbox_failed(self, event_id: str, error_msg: str) -> Result[None, Exception]:
        return Ok(None)

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

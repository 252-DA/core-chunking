from src.domain.entities.document import Document
from src.domain.ports.metadata_store import (
    DocumentFilter,
    IMetadataStore,
    IngestionStatus,
    OutboxEvent,
    StoredChunkMetadata,
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

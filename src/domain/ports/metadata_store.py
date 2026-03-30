from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
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
    ERROR = "ERROR"


class DocumentFilter:
    """Filter params cho metadata queries."""
    def __init__(
        self,
        doc_types: list[DocumentType] | None = None,
        language: str | None = None,
        uploaded_after: datetime | None = None,
        uploaded_before: datetime | None = None,
    ) -> None:
        self.doc_types = doc_types
        self.language = language
        self.uploaded_after = uploaded_after
        self.uploaded_before = uploaded_before


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


@dataclass(frozen=True)
class OutboxEvent:
    id: str
    event_type: str
    aggregate_id: str
    payload: dict
    status: str = "PENDING"
    attempts: int = 0
    error_msg: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


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
        """Xóa document metadata + chunks + outbox liên quan."""
        ...

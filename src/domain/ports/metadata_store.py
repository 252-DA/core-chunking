from abc import ABC, abstractmethod
from datetime import datetime

from src.domain.entities.document import Document, DocumentType


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


class IMetadataStore(ABC):
    """
    Port: lưu và query metadata của documents.
    Placeholder cho Phase 2 — trước mắt chưa implement.
    Sau này dùng PostgreSQL hoặc MongoDB để filter phức tạp
    trước khi search vector (CQRS pattern).
    """

    @abstractmethod
    def save(self, document: Document) -> None:
        """Lưu document metadata."""
        ...

    @abstractmethod
    def get(self, document_id: str) -> Document | None:
        """Lấy document theo id."""
        ...

    @abstractmethod
    def list(
        self,
        filters: DocumentFilter | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Document]:
        """List documents với optional filter."""
        ...

    @abstractmethod
    def delete(self, document_id: str) -> None:
        """Xóa document metadata."""
        ...

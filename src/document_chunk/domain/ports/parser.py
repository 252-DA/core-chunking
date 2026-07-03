from abc import ABC, abstractmethod
from pathlib import Path

from document_chunk.domain.entities.document import DocumentType, ParsedDocument
from document_chunk.shared.result import Result


class IParser(ABC):
    """
    Port: parse một file thành ParsedDocument (structured sections).
    Mỗi format (PDF, DOCX, PPTX) implement riêng.
    """

    @property
    @abstractmethod
    def supported_types(self) -> tuple[DocumentType, ...]:
        """Danh sách DocumentType mà parser này hỗ trợ."""
        ...

    @abstractmethod
    def parse(self, path: Path) -> Result[ParsedDocument, Exception]:
        """
        Parse file tại path → ParsedDocument.
        Không throw exception — trả về Err nếu thất bại.
        """
        ...

    def supports(self, doc_type: DocumentType) -> bool:
        return doc_type in self.supported_types

from abc import ABC, abstractmethod

from document_chunk.domain.entities.chunk import Chunk
from document_chunk.domain.entities.document import DocumentType, ParsedDocument
from document_chunk.shared.result import Result


class IChunker(ABC):
    """
    Port: chia ParsedDocument thành list[Chunk].
    Strategy pattern — swap chiến lược chunk khác nhau.
    """

    @property
    @abstractmethod
    def supported_types(self) -> tuple[DocumentType, ...]:
        """DocumentType mà chunker này tối ưu cho."""
        ...

    @abstractmethod
    def chunk(self, doc: ParsedDocument) -> Result[list[Chunk], Exception]:
        """
        Chunk document → list[Chunk].
        Không throw exception — trả về Err nếu thất bại.
        """
        ...

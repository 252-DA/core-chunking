from abc import ABC, abstractmethod

from src.domain.entities.chunk import Chunk
from src.domain.entities.embedding import Embedding
from src.domain.entities.search import SearchFilter, SearchResult
from src.shared.result import Result


class IVectorStore(ABC):
    """
    Port: lưu và search vector embeddings.
    Trước mắt: Qdrant. Sau này có thể swap sang Weaviate, Pinecone, ...
    """

    @abstractmethod
    def upsert(self, chunks: list[Chunk], embeddings: list[Embedding]) -> Result[None, Exception]:
        """
        Lưu chunks + embeddings vào store.
        Nếu chunk_id đã tồn tại → update.
        """
        ...

    @abstractmethod
    def search(
        self,
        query_vector: list[float],
        top_k: int,
        score_threshold: float = 0.0,
        filters: SearchFilter | None = None,
    ) -> Result[list[SearchResult], Exception]:
        """
        Vector similarity search.
        Trả về list[SearchResult] đã sort theo score descending.
        """
        ...

    @abstractmethod
    def delete(self, chunk_ids: list[str]) -> Result[None, Exception]:
        """Xóa chunks theo id."""
        ...

    @abstractmethod
    def delete_by_document(self, document_id: str) -> Result[None, Exception]:
        """Xóa toàn bộ chunks của một document."""
        ...

    @abstractmethod
    def count(self) -> Result[int, Exception]:
        """Tổng số chunks đang lưu."""
        ...

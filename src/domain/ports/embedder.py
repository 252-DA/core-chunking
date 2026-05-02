from abc import ABC, abstractmethod

from src.domain.entities.chunk import Chunk
from src.domain.entities.embedding import Embedding
from src.shared.result import Ok, Result


class IEmbedder(ABC):
    """
    Port: tạo vector embedding cho chunks.
    """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Tên model embedding đang dùng."""
        ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Số chiều của vector output."""
        ...

    @abstractmethod
    def embed(self, texts: list[str]) -> Result[list[list[float]], Exception]:
        """
        Embed list[str] → list[vector].
        Trả về list cùng length với input.
        Không throw exception — trả về Err nếu thất bại.
        """
        ...

    def embed_chunks(self, chunks: list[Chunk]) -> Result[list[Embedding], Exception]:
        """
        Convenience method: embed list[Chunk] → list[Embedding].
        Dùng embedding_input đã được enrich với heading context.
        """
        texts = [c.embedding_input for c in chunks]
        result = self.embed(texts)
        if result.is_err():
            return result
        embeddings = [
            Embedding(
                chunk_id=chunk.id,
                vector=tuple(vector),
                model=self.model_name,
                dimension=self.dimension,
            )
            for chunk, vector in zip(chunks, result.unwrap())
        ]
        return Ok(embeddings)

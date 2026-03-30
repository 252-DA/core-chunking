from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.shared.result import Result


@dataclass(frozen=True)
class GraphChunk:
    chunk_id: str
    chunk_index: int
    heading_path: tuple[str, ...] = ()


class IGraphStore(ABC):
    """
    Port: graph projection store (Neo4j).
    V1: heading graph first (Document -> Heading -> Chunk + NEXT edges).
    """

    @abstractmethod
    def upsert_heading_graph(
        self,
        document_id: str,
        course_id: str | None,
        owner_id: str | None,
        chunks: list[GraphChunk],
    ) -> Result[None, Exception]:
        ...

    @abstractmethod
    def delete_document(self, document_id: str) -> Result[None, Exception]:
        ...

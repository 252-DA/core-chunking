from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.domain.entities.document import DocumentType
from src.shared.result import Result


@dataclass(frozen=True)
class GraphChunk:
    chunk_id: str
    chunk_index: int
    heading_path: tuple[str, ...] = ()
    page_number: int | None = None
    language: str | None = None


@dataclass(frozen=True)
class GraphDocument:
    document_id: str
    document_name: str
    doc_type: DocumentType


@dataclass(frozen=True)
class GraphConcept:
    concept_id: str
    name: str
    canonical_name: str
    slug: str
    category: str = "other"
    language: str | None = None
    domain: str | None = None


@dataclass(frozen=True)
class GraphChunkConcept:
    chunk_id: str
    concept_id: str
    confidence: float = 1.0
    source: str = "heading"


class IGraphStore(ABC):
    """
    Port: graph projection store (Neo4j).
    V1: heading graph first (Document -> Heading -> Chunk + NEXT edges).
    """

    @abstractmethod
    def upsert_heading_graph(
        self,
        document: GraphDocument,
        course_id: str | None,
        owner_id: str | None,
        chunks: list[GraphChunk],
    ) -> Result[None, Exception]:
        ...

    @abstractmethod
    def upsert_concept_graph(
        self,
        document_id: str,
        concepts: list[GraphConcept],
        mentions: list[GraphChunkConcept],
    ) -> Result[None, Exception]:
        ...

    @abstractmethod
    def delete_document(self, document_id: str) -> Result[None, Exception]:
        ...

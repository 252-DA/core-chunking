from abc import ABC, abstractmethod
from dataclasses import dataclass

from document_chunk.domain.entities.document import DocumentType
from document_chunk.shared.result import Result


@dataclass(frozen=True)
class GraphChunkLOEdge:
    chunk_id: str
    lo_id: str
    confidence: float
    source: str


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

    @abstractmethod
    def upsert_chunk_lo_mappings(
        self,
        mappings: "list[GraphChunkLOEdge]",
    ) -> Result[None, Exception]:
        """Upsert Chunk -[:SUPPORTS]-> LearningOutcome edges."""
        ...

    @abstractmethod
    def find_chunks_for_lo(
        self, lo_id: str, limit: int = 20
    ) -> Result[list[str], Exception]:
        """Trả về chunk_ids hỗ trợ lo_id (qua SUPPORTS edge), sort theo confidence."""
        ...

    @abstractmethod
    def find_chunks_for_chapter(
        self, course_id: str, chapter_code: str, limit: int = 20
    ) -> Result[list[str], Exception]:
        """Trả về chunk_ids liên quan chapter (union EXPLAINS + SUPPORTS)."""
        ...

    @abstractmethod
    def find_los_for_assessment(
        self, course_id: str, assessment_code: str
    ) -> Result[list[str], Exception]:
        """Trả về lo_ids được evaluate bởi assessment_code."""
        ...

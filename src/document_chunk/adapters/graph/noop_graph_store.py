from document_chunk.domain.ports.graph_store import (
    GraphAssessment,
    GraphChapter,
    GraphChunk,
    GraphChunkConcept,
    GraphChunkLOEdge,
    GraphConcept,
    GraphDocument,
    GraphLO,
    IGraphStore,
)
from document_chunk.shared.result import Ok, Result


class NoopGraphStore(IGraphStore):
    def upsert_heading_graph(
        self,
        document: GraphDocument,
        course_id: str | None,
        owner_id: str | None,
        chunks: list[GraphChunk],
    ) -> Result[None, Exception]:
        return Ok(None)

    def upsert_concept_graph(
        self,
        document_id: str,
        concepts: list[GraphConcept],
        mentions: list[GraphChunkConcept],
    ) -> Result[None, Exception]:
        return Ok(None)

    def delete_document(self, document_id: str) -> Result[None, Exception]:
        return Ok(None)

    def upsert_curriculum_graph(
        self,
        course_id: str,
        course_code: str,
        course_title_vi: str,
        chapters: list[GraphChapter],
        los: list[GraphLO],
        assessments: list[GraphAssessment],
        lo_assessment_links: list[tuple[str, str]],
    ) -> Result[None, Exception]:
        return Ok(None)

    def upsert_chunk_lo_mappings(
        self,
        mappings: list[GraphChunkLOEdge],
    ) -> Result[None, Exception]:
        return Ok(None)

    def find_chunks_for_lo(
        self, lo_id: str, limit: int = 20
    ) -> Result[list[str], Exception]:
        return Ok([])

    def find_chunks_for_chapter(
        self, course_id: str, chapter_code: str, limit: int = 20
    ) -> Result[list[str], Exception]:
        return Ok([])

    def find_los_for_assessment(
        self, course_id: str, assessment_code: str
    ) -> Result[list[str], Exception]:
        return Ok([])

from src.domain.ports.graph_store import (
    GraphChunk,
    GraphChunkConcept,
    GraphConcept,
    GraphDocument,
    IGraphStore,
)
from src.shared.result import Ok, Result


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

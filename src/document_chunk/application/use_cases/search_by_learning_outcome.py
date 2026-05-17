"""
SearchByLearningOutcomeUseCase — graph-traversal retrieval by LO/chapter/assessment.

MVP: graph traversal only, no embedding rerank.
Phase 2: + Qdrant id-filter cosine rerank when query is given.
"""
from dataclasses import dataclass

from document_chunk.domain.exceptions import ProcessingError
from document_chunk.domain.ports.graph_store import IGraphStore
from document_chunk.domain.ports.metadata_store import IMetadataStore, StoredChunkMetadata
from document_chunk.domain.ports.vector_store import IVectorStore
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)


@dataclass
class SearchByLearningOutcomeRequest:
    course_id: str
    lo_code: str | None = None
    chapter_code: str | None = None
    assessment_code: str | None = None
    query: str | None = None
    top_k: int = 10


@dataclass
class LOSearchResultItem:
    chunk_id: str
    document_id: str
    heading_path: tuple[str, ...]
    content_text: str | None
    page_number: int | None
    lo_ids: list[str]
    rank: int


@dataclass
class SearchByLearningOutcomeResponse:
    course_id: str
    lo_ids_searched: list[str]
    results: list[LOSearchResultItem]
    total_found: int


class SearchByLearningOutcomeUseCase:
    def __init__(
        self,
        metadata_store: IMetadataStore,
        graph_store: IGraphStore,
    ) -> None:
        self._metadata_store = metadata_store
        self._graph_store = graph_store

    def execute(
        self, request: SearchByLearningOutcomeRequest
    ) -> Result[SearchByLearningOutcomeResponse, Exception]:
        # 1. Resolve target → set of lo_ids
        lo_ids = self._resolve_lo_ids(request)
        if lo_ids is None:
            return Err(ProcessingError(
                "One of lo_code, chapter_code, or assessment_code is required"
            ))
        if lo_ids.is_err():
            return Err(lo_ids.error)

        target_lo_ids: list[str] = lo_ids.unwrap()
        if not target_lo_ids:
            return Ok(SearchByLearningOutcomeResponse(
                course_id=request.course_id,
                lo_ids_searched=[],
                results=[],
                total_found=0,
            ))

        # 2. Collect chunks via Neo4j SUPPORTS edges
        chunk_id_set: set[str] = set()
        for lo_id in target_lo_ids:
            result = self._graph_store.find_chunks_for_lo(lo_id, limit=request.top_k)
            if result.is_ok():
                chunk_id_set.update(result.unwrap())

        if not chunk_id_set:
            return Ok(SearchByLearningOutcomeResponse(
                course_id=request.course_id,
                lo_ids_searched=target_lo_ids,
                results=[],
                total_found=0,
            ))

        # 3. Fetch chunk metadata from Postgres
        chunk_lo_map: dict[str, list[str]] = {}
        for lo_id in target_lo_ids:
            chunks_result = self._metadata_store.list_chunks_for_lo(lo_id)
            if chunks_result.is_ok():
                for chunk in chunks_result.unwrap():
                    chunk_lo_map.setdefault(chunk.chunk_id, []).append(lo_id)

        all_chunk_ids = sorted(chunk_id_set)
        results: list[LOSearchResultItem] = []
        for rank, chunk_id in enumerate(all_chunk_ids[: request.top_k], start=1):
            lo_ids_for_chunk = chunk_lo_map.get(chunk_id, [])
            # Fetch chunk content from Postgres if not already loaded
            chunks_result = self._metadata_store.list_chunks_for_lo(
                lo_ids_for_chunk[0] if lo_ids_for_chunk else target_lo_ids[0]
            )
            chunk_data: StoredChunkMetadata | None = None
            if chunks_result.is_ok():
                for c in chunks_result.unwrap():
                    if c.chunk_id == chunk_id:
                        chunk_data = c
                        break

            results.append(LOSearchResultItem(
                chunk_id=chunk_id,
                document_id=chunk_data.document_id if chunk_data else "",
                heading_path=chunk_data.heading_path if chunk_data else (),
                content_text=chunk_data.content_text if chunk_data else None,
                page_number=chunk_data.page_number if chunk_data else None,
                lo_ids=lo_ids_for_chunk,
                rank=rank,
            ))

        return Ok(SearchByLearningOutcomeResponse(
            course_id=request.course_id,
            lo_ids_searched=target_lo_ids,
            results=results,
            total_found=len(results),
        ))

    def _resolve_lo_ids(
        self, request: SearchByLearningOutcomeRequest
    ) -> Result[list[str], Exception] | None:
        course_id = request.course_id

        if request.lo_code:
            lo_id = f"{course_id}:{request.lo_code}"
            return Ok([lo_id])

        if request.chapter_code:
            result = self._metadata_store.list_los_by_chapter(course_id, request.chapter_code)
            if result.is_err():
                return Err(result.error)
            return Ok([lo.lo_id for lo in result.unwrap()])

        if request.assessment_code:
            result = self._metadata_store.list_los_by_assessment(course_id, request.assessment_code)
            if result.is_err():
                return Err(result.error)
            return Ok([lo.lo_id for lo in result.unwrap()])

        return None

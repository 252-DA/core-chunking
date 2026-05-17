"""
SearchChunksUseCase — embed query → search Qdrant → return ranked results.

Flow:
  SearchRequest → [embed query] → [vector search + filter] → SearchResponse
"""
import time

from document_chunk.application.dto.search_dto import SearchRequest, SearchResponse, SearchResultItem
from document_chunk.domain.entities.search import SearchFilter
from document_chunk.domain.ports.embedder import IEmbedder
from document_chunk.domain.ports.vector_store import IVectorStore
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)


class SearchChunksUseCase:
    def __init__(self, embedder: IEmbedder, vector_store: IVectorStore) -> None:
        self._embedder = embedder
        self._vector_store = vector_store

    def execute(self, request: SearchRequest) -> Result[SearchResponse, Exception]:
        started_at = time.perf_counter()

        with tracer.start_as_current_span("search_chunks") as span:
            span.set_attribute("query", request.query)
            span.set_attribute("top_k", request.top_k)

            logger.info("search.started", query=request.query, top_k=request.top_k)

            # 1. Embed query
            with tracer.start_as_current_span("embed_query"):
                embed_result = self._embedder.embed([request.query])
                if embed_result.is_err():
                    logger.error("search.embed_failed", error=str(embed_result.error))
                    return embed_result

            query_vector = embed_result.unwrap()[0]

            # 2. Build filter từ request
            filters = SearchFilter(
                doc_types=tuple(request.doc_types),
                document_ids=tuple(request.document_ids),
                language=request.language,
                course_id=request.course_id,
                owner_id=request.owner_id,
            )

            # 3. Search vector store
            with tracer.start_as_current_span("vector_search"):
                search_result = self._vector_store.search(
                    query_vector=query_vector,
                    top_k=request.top_k,
                    score_threshold=request.score_threshold,
                    filters=filters if self._has_filters(filters) else None,
                )
                if search_result.is_err():
                    logger.error("search.vector_failed", error=str(search_result.error))
                    return search_result

            results = search_result.unwrap()
            duration_ms = (time.perf_counter() - started_at) * 1000

            span.set_attribute("results.count", len(results))
            span.set_attribute("duration_ms", duration_ms)

            logger.info(
                "search.completed",
                query=request.query,
                found=len(results),
                duration_ms=round(duration_ms, 2),
            )

            # 4. Map domain entities → DTOs
            return Ok(
                SearchResponse(
                    query=request.query,
                    results=[
                        SearchResultItem(
                            chunk_id=r.chunk.id,
                            document_id=r.chunk.metadata.document_id,
                            document_name=r.chunk.metadata.document_name,
                            doc_type=r.chunk.metadata.document_type,
                            heading_path=list(r.chunk.metadata.heading_path),
                            content=r.chunk.content,
                            score=r.score,
                            rank=r.rank,
                            page_number=r.chunk.metadata.page_number,
                        )
                        for r in results
                    ],
                    total_found=len(results),
                    search_time_ms=round(duration_ms, 2),
                )
            )

    def _has_filters(self, filters: SearchFilter) -> bool:
        return bool(
            filters.doc_types
            or filters.document_ids
            or filters.language
            or filters.course_id
            or filters.owner_id
            or filters.uploaded_after
            or filters.uploaded_before
        )

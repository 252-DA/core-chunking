"""Retrieve grounded learning context for quiz generation.

The use case keeps retrieval deterministic and independent from MCP. Delivery
adapters (MCP, HTTP, gRPC) can expose it without leaking database or vector
store details to model-facing clients.
"""

from dataclasses import dataclass

from document_chunk.application.dto.search_dto import SearchRequest
from document_chunk.application.use_cases.search_chunks import SearchChunksUseCase
from document_chunk.domain.exceptions import ProcessingError
from document_chunk.domain.ports.metadata_store import IMetadataStore, StoredChunkMetadata
from document_chunk.shared.result import Err, Ok, Result


@dataclass(frozen=True)
class RetrieveQuizContextRequest:
    course_id: str
    lo_code: str
    query: str | None = None
    bloom_level: str | None = None
    assessment_style: str = "quiz"
    top_k: int = 5


@dataclass(frozen=True)
class QuizContextChunk:
    chunk_id: str
    document_id: str
    content: str
    heading_path: tuple[str, ...]
    page_number: int | None
    rank: int
    score: float | None = None


@dataclass(frozen=True)
class RetrieveQuizContextResponse:
    course_id: str
    lo_id: str
    lo_code: str
    lo_statement: str
    bloom_level: str
    assessment_style: str
    query: str
    chunks: tuple[QuizContextChunk, ...]
    total_chars: int


class RetrieveQuizContextUseCase:
    """Resolve an LO and return bounded, source-addressable supporting chunks."""

    def __init__(
        self,
        metadata_store: IMetadataStore,
        semantic_search: SearchChunksUseCase | None = None,
        max_chunk_chars: int = 1600,
        max_total_chars: int = 8000,
    ) -> None:
        self._metadata_store = metadata_store
        self._semantic_search = semantic_search
        self._max_chunk_chars = max_chunk_chars
        self._max_total_chars = max_total_chars

    def execute(
        self,
        request: RetrieveQuizContextRequest,
    ) -> Result[RetrieveQuizContextResponse, Exception]:
        if not request.course_id.strip():
            return Err(ProcessingError("course_id is required"))
        if not request.lo_code.strip():
            return Err(ProcessingError("lo_code is required"))
        if request.top_k < 1 or request.top_k > 20:
            return Err(ProcessingError("top_k must be between 1 and 20"))

        curriculum_result = self._metadata_store.get_curriculum(request.course_id)
        if curriculum_result.is_err():
            return Err(curriculum_result.error)
        curriculum = curriculum_result.unwrap()
        if curriculum is None:
            return Err(ProcessingError(f"No curriculum for course {request.course_id}"))

        _, _, learning_outcomes, _ = curriculum
        normalized_code = self._normalize_lo_code(request.lo_code)
        lo = next(
            (
                item
                for item in learning_outcomes
                if item.code == normalized_code
                or item.lo_id == request.lo_code
                or item.lo_id == f"{request.course_id}:{normalized_code}"
            ),
            None,
        )
        if lo is None:
            return Err(
                ProcessingError(
                    f"Learning outcome {request.lo_code!r} not found in course "
                    f"{request.course_id}"
                )
            )

        chunks_result = self._metadata_store.list_chunks_for_lo(lo.lo_id)
        if chunks_result.is_err():
            return Err(chunks_result.error)
        mapped_chunks = [
            chunk
            for chunk in chunks_result.unwrap()
            if (chunk.content_text or "").strip()
        ]

        bloom = str(request.bloom_level or lo.bloom_level or "understand")
        query = (request.query or "").strip() or self._default_query(
            lo.statement_vi,
            lo.statement_en,
            bloom,
            request.assessment_style,
        )
        ordered_chunks, scores = self._semantic_order(
            course_id=request.course_id,
            query=query,
            mapped_chunks=mapped_chunks,
            top_k=request.top_k,
        )
        selected, total_chars = self._bound_context(ordered_chunks, request.top_k)

        response_chunks = tuple(
            QuizContextChunk(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                content=self._chunk_text(chunk)[: self._max_chunk_chars].rstrip(),
                heading_path=chunk.heading_path,
                page_number=chunk.page_number,
                rank=index,
                score=scores.get(chunk.chunk_id),
            )
            for index, chunk in enumerate(selected, start=1)
        )
        return Ok(
            RetrieveQuizContextResponse(
                course_id=request.course_id,
                lo_id=lo.lo_id,
                lo_code=lo.code,
                lo_statement=lo.statement_vi,
                bloom_level=bloom,
                assessment_style=request.assessment_style,
                query=query,
                chunks=response_chunks,
                total_chars=total_chars,
            )
        )

    def _semantic_order(
        self,
        course_id: str,
        query: str,
        mapped_chunks: list[StoredChunkMetadata],
        top_k: int,
    ) -> tuple[list[StoredChunkMetadata], dict[str, float]]:
        if self._semantic_search is None or not mapped_chunks:
            return mapped_chunks, {}

        search_result = self._semantic_search.execute(
            SearchRequest(
                query=query,
                course_id=course_id,
                top_k=min(100, max(top_k * 5, 20)),
            )
        )
        if search_result.is_err():
            return mapped_chunks, {}

        ranked = search_result.unwrap().results
        scores = {item.chunk_id: item.score for item in ranked}
        semantic_rank = {item.chunk_id: item.rank for item in ranked}
        original_rank = {chunk.chunk_id: index for index, chunk in enumerate(mapped_chunks)}
        ordered = sorted(
            mapped_chunks,
            key=lambda chunk: (
                0 if chunk.chunk_id in semantic_rank else 1,
                semantic_rank.get(chunk.chunk_id, original_rank[chunk.chunk_id]),
                original_rank[chunk.chunk_id],
            ),
        )
        return ordered, scores

    def _bound_context(
        self,
        chunks: list[StoredChunkMetadata],
        top_k: int,
    ) -> tuple[list[StoredChunkMetadata], int]:
        selected: list[StoredChunkMetadata] = []
        total_chars = 0
        for chunk in chunks:
            bounded_length = min(len(self._chunk_text(chunk)), self._max_chunk_chars)
            if bounded_length == 0:
                continue
            if selected and total_chars + bounded_length > self._max_total_chars:
                break
            selected.append(chunk)
            total_chars += bounded_length
            if len(selected) >= top_k:
                break
        return selected, total_chars

    @staticmethod
    def _normalize_lo_code(value: str) -> str:
        code = value.strip()
        if ":" in code:
            code = code.rsplit(":", 1)[-1]
        return code if code.startswith("L.O.") else f"L.O.{code}"

    @staticmethod
    def _default_query(
        statement_vi: str,
        statement_en: str | None,
        bloom_level: str,
        assessment_style: str,
    ) -> str:
        statements = " / ".join(
            item.strip() for item in (statement_vi, statement_en or "") if item.strip()
        )
        return (
            f"{statements}. Bloom level: {bloom_level}. "
            f"Assessment style: {assessment_style}."
        )

    @staticmethod
    def _chunk_text(chunk: StoredChunkMetadata) -> str:
        return (chunk.content_text or "").strip()

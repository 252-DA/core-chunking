"""
PipelineCore — shared parse → chunk → embed → upsert + outbox append.

Used by both ProcessDocumentUseCase (gRPC sync path) and RunPipelineUseCase (async worker path).
Callers are responsible for:
  - Document entity creation
  - MinIO upload (or skipping it for the worker path)
  - QUEUED status initialisation
  - ACTIVE_REQUESTS gauge management
"""
import time
from dataclasses import dataclass

from document_chunk.domain.entities.chunk import Chunk
from document_chunk.domain.entities.document import Document, DocumentType
from document_chunk.domain.exceptions import ProcessingError, UnsupportedFileTypeError
from document_chunk.domain.ports.chunker import IChunker
from document_chunk.domain.ports.embedder import IEmbedder
from document_chunk.domain.ports.graph_store import GraphChunk
from document_chunk.domain.ports.metadata_store import (
    IMetadataStore,
    IngestionStatus,
    StoredChunkMetadata,
)
from document_chunk.domain.ports.parser import IParser
from document_chunk.domain.ports.preprocessor import IPreprocessor
from document_chunk.domain.ports.vector_store import IVectorStore
from document_chunk.shared.logger import get_logger
from document_chunk.shared.metrics import (
    CHUNKS_CREATED,
    CHUNK_SIZE,
    DOCUMENTS_PROCESSED,
    PROCESSING_DURATION,
)
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_EVENT_HEADING_GRAPH_PROJECT = "heading_graph_project"


@dataclass
class PipelineCoreResult:
    chunks: list[Chunk]
    duration_ms: float


class PipelineCore:
    """
    Orchestrates parse → chunk → embed → upsert, then appends outbox events for graph projection.

    Handles per-stage status updates, tracing spans, and metrics.
    Does NOT manage ACTIVE_REQUESTS — that belongs to the calling use case.
    """

    def __init__(
        self,
        parsers: list[IParser],
        chunker: IChunker,
        embedder: IEmbedder,
        vector_store: IVectorStore,
        metadata_store: IMetadataStore,
        preprocessors: list[IPreprocessor] | None = None,
    ) -> None:
        self._parsers = parsers
        self._chunker = chunker
        self._embedder = embedder
        self._vector_store = vector_store
        self._metadata_store = metadata_store
        self._preprocessors = preprocessors or []

    def run(
        self,
        document: Document,
        metadata: dict,
        language: str | None = None,
    ) -> Result[PipelineCoreResult, Exception]:
        """
        Run the full ingestion pipeline for an already-created Document entity.

        file_path is taken from document.path.
        Updates document status at each stage via metadata_store.
        """
        document_id = document.id
        doc_type = document.doc_type
        file_path = document.path
        if file_path is None:
            return self._fail(
                document_id,
                doc_type,
                ProcessingError("PipelineCore requires document.path for local file access"),
            )
        original_file_path = file_path
        started_at = time.perf_counter()

        try:
            # 1. Status → PARSING
            r = self._metadata_store.update_document_status(document_id, IngestionStatus.PARSING)
            if r.is_err():
                return self._fail(document_id, doc_type, r.error)

            # 2. Preprocessors (OCR, etc.) — applied sequentially
            try:
                for preprocessor in self._preprocessors:
                    if preprocessor.should_apply(file_path):
                        file_path = preprocessor.process(file_path)
            except Exception as exc:
                logger.warning(
                    "pipeline.preprocessor_failed",
                    document_id=document_id,
                    preprocessor=preprocessor.__class__.__name__,
                    file_path=str(file_path),
                    fallback_path=str(original_file_path),
                    error=str(exc),
                )
                file_path = original_file_path

            # 3. Parse
            _t = time.perf_counter()
            with tracer.start_as_current_span("parse"):
                parser = self._resolve_parser(doc_type)
                if parser is None:
                    return self._fail(document_id, doc_type, UnsupportedFileTypeError(doc_type.value))
                parse_result = parser.parse(file_path)
                if parse_result.is_err():
                    logger.error("pipeline.parse_failed", document_id=document_id, error=str(parse_result.error))
                    return self._fail(document_id, doc_type, parse_result.error)
            PROCESSING_DURATION.labels(stage="parse", doc_type=doc_type.value).observe(
                time.perf_counter() - _t
            )

            parsed_doc = parse_result.unwrap()
            parsed_doc.document = document
            if language:
                parsed_doc.language = language
            if metadata:
                parsed_doc.metadata = {**parsed_doc.metadata, **metadata}

            logger.debug(
                "pipeline.parsed",
                document_id=document_id,
                sections=len(parsed_doc.sections),
                pages=parsed_doc.page_count,
            )

            # 4. Status → CHUNKING
            r = self._metadata_store.update_document_status(document_id, IngestionStatus.CHUNKING)
            if r.is_err():
                return self._fail(document_id, doc_type, r.error)

            # 5. Chunk
            _t = time.perf_counter()
            with tracer.start_as_current_span("chunk"):
                chunk_result = self._chunker.chunk(parsed_doc)
                if chunk_result.is_err():
                    logger.error("pipeline.chunk_failed", document_id=document_id, error=str(chunk_result.error))
                    return self._fail(document_id, doc_type, chunk_result.error)
            PROCESSING_DURATION.labels(stage="chunk", doc_type=doc_type.value).observe(
                time.perf_counter() - _t
            )

            chunks = chunk_result.unwrap()
            CHUNKS_CREATED.labels(doc_type=doc_type.value).inc(len(chunks))
            for c in chunks:
                CHUNK_SIZE.labels(doc_type=doc_type.value).observe(len(c.content))
            logger.debug("pipeline.chunked", document_id=document_id, chunks=len(chunks))

            # 6. Status → EMBEDDING
            r = self._metadata_store.update_document_status(document_id, IngestionStatus.EMBEDDING)
            if r.is_err():
                return self._fail(document_id, doc_type, r.error)

            # 7. Embed
            _t = time.perf_counter()
            with tracer.start_as_current_span("embed"):
                embed_result = self._embedder.embed_chunks(chunks)
                if embed_result.is_err():
                    logger.error("pipeline.embed_failed", document_id=document_id, error=str(embed_result.error))
                    return self._fail(document_id, doc_type, embed_result.error)
            PROCESSING_DURATION.labels(stage="embed", doc_type=doc_type.value).observe(
                time.perf_counter() - _t
            )

            embeddings = embed_result.unwrap()

            # 8. Upsert to vector store while document remains EMBEDDING.
            _t = time.perf_counter()
            with tracer.start_as_current_span("upsert"):
                upsert_result = self._vector_store.upsert(chunks, embeddings)
                if upsert_result.is_err():
                    logger.error("pipeline.upsert_failed", document_id=document_id, error=str(upsert_result.error))
                    return self._fail(document_id, doc_type, upsert_result.error)
            PROCESSING_DURATION.labels(stage="upsert", doc_type=doc_type.value).observe(
                time.perf_counter() - _t
            )

            # 10. Persist chunk metadata and append the heading projection outbox event atomically.
            graph_chunks = self._build_graph_chunks(chunks)
            outbox_result = self._metadata_store.upsert_chunks_with_outbox(
                chunks=self._build_chunk_metadata(chunks),
                event_type=_EVENT_HEADING_GRAPH_PROJECT,
                aggregate_id=document_id,
                payload={
                    "document_id": document_id,
                    "document_name": document.name,
                    "doc_type": doc_type.value,
                    "course_id": metadata.get("course_id"),
                    "owner_id": metadata.get("owner_id"),
                    "chunks": [
                        {
                            "chunk_id": c.chunk_id,
                            "chunk_index": c.chunk_index,
                            "heading_path": list(c.heading_path),
                            "page_number": c.page_number,
                            "language": c.language,
                        }
                        for c in graph_chunks
                    ],
                },
            )
            if outbox_result.is_err():
                return self._fail(document_id, doc_type, outbox_result.error)

            # 12. Status → INDEXED. Content generation is a separate explicit request.
            status_result = self._metadata_store.update_document_status(
                document_id, IngestionStatus.INDEXED
            )
            if status_result.is_err():
                return self._fail(document_id, doc_type, status_result.error)

            duration_ms = (time.perf_counter() - started_at) * 1000
            DOCUMENTS_PROCESSED.labels(status="success", doc_type=doc_type.value).inc()

            return Ok(PipelineCoreResult(chunks=chunks, duration_ms=duration_ms))

        except Exception as exc:
            logger.error("pipeline.unhandled_exception", document_id=document_id, error=str(exc))
            return self._fail(document_id, doc_type, exc)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_parser(self, doc_type: DocumentType) -> IParser | None:
        for parser in self._parsers:
            if parser.supports(doc_type):
                return parser
        return None

    def _build_chunk_metadata(self, chunks: list[Chunk]) -> list[StoredChunkMetadata]:
        return [
            StoredChunkMetadata(
                chunk_id=chunk.id,
                document_id=chunk.metadata.document_id,
                chunk_index=chunk.metadata.chunk_index,
                heading_path=chunk.metadata.heading_path,
                heading_level=chunk.metadata.heading_level,
                page_number=chunk.metadata.page_number,
                content_length=len(chunk.content),
                language=chunk.metadata.language,
                content_text=chunk.content,
                embedding_input=chunk.embedding_input,
            )
            for chunk in chunks
        ]

    def _build_graph_chunks(self, chunks: list[Chunk]) -> list[GraphChunk]:
        return [
            GraphChunk(
                chunk_id=chunk.id,
                chunk_index=chunk.metadata.chunk_index,
                heading_path=chunk.metadata.heading_path,
                page_number=chunk.metadata.page_number,
                language=chunk.metadata.language,
            )
            for chunk in chunks
        ]

    def _fail(
        self, document_id: str, doc_type: DocumentType, error: Exception
    ) -> Result[PipelineCoreResult, Exception]:
        DOCUMENTS_PROCESSED.labels(status="failed", doc_type=doc_type.value).inc()
        self._metadata_store.update_document_status(
            document_id=document_id,
            status=IngestionStatus.ERROR,
            error_msg=str(error),
        )
        return Err(error)

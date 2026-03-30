"""
ProcessDocumentUseCase — orchestrate toàn bộ pipeline xử lý document.

Flow:
  File → [detect type] → [upload MinIO] → [preprocessors] → [parse]
       → [chunk] → [embed] → [upsert Qdrant] → ProcessDocumentResponse
"""
import time
import uuid
from pathlib import Path

from src.application.dto.document_dto import (
    ChunkSummary,
    ProcessDocumentRequest,
    ProcessDocumentResponse,
)
from src.domain.entities.chunk import Chunk
from src.domain.entities.document import Document, DocumentType, ParsedDocument
from src.domain.ports.chunker import IChunker
from src.domain.ports.embedder import IEmbedder
from src.domain.ports.file_storage import IFileStorage
from src.domain.ports.graph_store import GraphChunk, IGraphStore
from src.domain.ports.metadata_store import (
    IMetadataStore,
    IngestionStatus,
    StoredChunkMetadata,
)
from src.domain.ports.parser import IParser
from src.domain.ports.preprocessor import IPreprocessor
from src.domain.ports.vector_store import IVectorStore
from src.domain.exceptions import UnsupportedFileTypeError
from src.shared.logger import get_logger
from src.shared.result import Err, Ok, Result
from src.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

# Extension → DocumentType
_EXT_MAP: dict[str, DocumentType] = {
    ".pdf": DocumentType.PDF,
    ".docx": DocumentType.DOCX,
    ".doc": DocumentType.DOCX,
    ".pptx": DocumentType.PPTX,
    ".ppt": DocumentType.PPTX,
    ".md": DocumentType.MARKDOWN,
    ".markdown": DocumentType.MARKDOWN,
}

_META_KEY_ALIASES: dict[str, str] = {
    "courseid": "course_id",
    "ownerid": "owner_id",
    "sourcefilename": "source_file_name",
    "filename": "source_file_name",
    "originalfilename": "source_file_name",
}

_EVENT_HEADING_GRAPH_PROJECT = "heading_graph_project"


class ProcessDocumentUseCase:
    def __init__(
        self,
        parsers: list[IParser],
        chunker: IChunker,
        embedder: IEmbedder,
        vector_store: IVectorStore,
        file_storage: IFileStorage,
        metadata_store: IMetadataStore,
        graph_store: IGraphStore,
        preprocessors: list[IPreprocessor] | None = None,
    ) -> None:
        self._parsers = parsers
        self._chunker = chunker
        self._embedder = embedder
        self._vector_store = vector_store
        self._file_storage = file_storage
        self._metadata_store = metadata_store
        self._graph_store = graph_store
        self._preprocessors = preprocessors or []

    def execute(
        self, request: ProcessDocumentRequest
    ) -> Result[ProcessDocumentResponse, Exception]:
        started_at = time.perf_counter()
        document_id = request.document_id or str(uuid.uuid4())
        metadata = self._normalize_metadata(request.metadata)
        source_file_name = (
            request.original_file_name.strip()
            if request.original_file_name and request.original_file_name.strip()
            else request.file_path.name
        )
        source_file_name = Path(source_file_name).name or request.file_path.name
        metadata["source_file_name"] = source_file_name
        if request.language and request.language.strip():
            metadata["language"] = request.language.strip()

        with tracer.start_as_current_span("process_document") as span:
            span.set_attribute("document.id", document_id)
            span.set_attribute("file.path", str(request.file_path))

            logger.info(
                "process_document.started",
                document_id=document_id,
                file=source_file_name,
            )

            # 1. Detect document type
            doc_type = request.doc_type or self._detect_type(request.file_path)
            if doc_type is None:
                return Err(UnsupportedFileTypeError(request.file_path.suffix))

            span.set_attribute("document.type", doc_type.value)

            try:
                document = Document(
                    id=document_id,
                    name=source_file_name,
                    path=request.file_path,
                    doc_type=doc_type,
                    size_bytes=request.file_path.stat().st_size,
                    mime_type=self._mime_type(doc_type),
                )

                storage_key = f"{doc_type.value}/{document_id}/{document.name}"
                metadata["storage_key"] = storage_key

                # SQL source of truth: luôn upsert record trước khi chạy pipeline.
                store_doc_result = self._metadata_store.upsert_document(
                    document=document,
                    metadata=metadata,
                    status=IngestionStatus.QUEUED,
                )
                if store_doc_result.is_err():
                    return Err(store_doc_result.error)

                # 3. Upload raw file to object storage
                upload_result = self._file_storage.upload(request.file_path, storage_key)
                if upload_result.is_err():
                    logger.error(
                        "process_document.upload_failed",
                        document_id=document_id,
                        error=str(upload_result.error),
                    )
                    return self._fail(document_id, upload_result.error)

                logger.debug("process_document.uploaded", document_id=document_id, key=storage_key)

                parsing_status = self._metadata_store.update_document_status(
                    document_id=document_id,
                    status=IngestionStatus.PARSING,
                )
                if parsing_status.is_err():
                    return self._fail(document_id, parsing_status.error)

                # 4. Preprocessors (OCR, v.v.) — áp dụng tuần tự nếu cần
                file_path = request.file_path
                for preprocessor in self._preprocessors:
                    if preprocessor.should_apply(file_path):
                        logger.debug(
                            "process_document.preprocessing",
                            preprocessor=type(preprocessor).__name__,
                        )
                        file_path = preprocessor.process(file_path)

                # 5. Parse
                with tracer.start_as_current_span("parse"):
                    parser = self._resolve_parser(doc_type)
                    if parser is None:
                        return self._fail(document_id, UnsupportedFileTypeError(doc_type.value))

                    parse_result = parser.parse(file_path)
                    if parse_result.is_err():
                        logger.error(
                            "process_document.parse_failed",
                            document_id=document_id,
                            error=str(parse_result.error),
                        )
                        return self._fail(document_id, parse_result.error)

                parsed_doc = parse_result.unwrap()
                self._align_parsed_document(
                    parsed_doc=parsed_doc,
                    document=document,
                    language=metadata.get("language"),
                    metadata=metadata,
                )
                logger.debug(
                    "process_document.parsed",
                    document_id=document_id,
                    sections=len(parsed_doc.sections),
                    pages=parsed_doc.page_count,
                )

                chunking_status = self._metadata_store.update_document_status(
                    document_id=document_id,
                    status=IngestionStatus.CHUNKING,
                )
                if chunking_status.is_err():
                    return self._fail(document_id, chunking_status.error)

                # 6. Chunk
                with tracer.start_as_current_span("chunk"):
                    chunk_result = self._chunker.chunk(parsed_doc)
                    if chunk_result.is_err():
                        logger.error(
                            "process_document.chunk_failed",
                            document_id=document_id,
                            error=str(chunk_result.error),
                        )
                        return self._fail(document_id, chunk_result.error)

                chunks = chunk_result.unwrap()
                logger.debug("process_document.chunked", document_id=document_id, chunks=len(chunks))

                embedding_status = self._metadata_store.update_document_status(
                    document_id=document_id,
                    status=IngestionStatus.EMBEDDING,
                )
                if embedding_status.is_err():
                    return self._fail(document_id, embedding_status.error)

                # 7. Embed
                with tracer.start_as_current_span("embed"):
                    embed_result = self._embedder.embed_chunks(chunks)
                    if embed_result.is_err():
                        logger.error(
                            "process_document.embed_failed",
                            document_id=document_id,
                            error=str(embed_result.error),
                        )
                        return self._fail(document_id, embed_result.error)

                embeddings = embed_result.unwrap()

                upserting_status = self._metadata_store.update_document_status(
                    document_id=document_id,
                    status=IngestionStatus.UPSERTING,
                )
                if upserting_status.is_err():
                    return self._fail(document_id, upserting_status.error)

                # 8. Upsert to vector store
                with tracer.start_as_current_span("upsert"):
                    upsert_result = self._vector_store.upsert(chunks, embeddings)
                    if upsert_result.is_err():
                        logger.error(
                            "process_document.upsert_failed",
                            document_id=document_id,
                            error=str(upsert_result.error),
                        )
                        return self._fail(document_id, upsert_result.error)

                chunk_metadata_result = self._metadata_store.upsert_chunks(
                    self._build_chunk_metadata(chunks),
                )
                if chunk_metadata_result.is_err():
                    return self._fail(document_id, chunk_metadata_result.error)

                graph_chunks = self._build_graph_chunks(chunks)
                outbox_payload = {
                    "document_id": document_id,
                    "course_id": metadata.get("course_id"),
                    "owner_id": metadata.get("owner_id"),
                    "chunks": [
                        {
                            "chunk_id": c.chunk_id,
                            "chunk_index": c.chunk_index,
                            "heading_path": list(c.heading_path),
                        }
                        for c in graph_chunks
                    ],
                }
                outbox_result = self._metadata_store.append_outbox_event(
                    event_type=_EVENT_HEADING_GRAPH_PROJECT,
                    aggregate_id=document_id,
                    payload=outbox_payload,
                )
                if outbox_result.is_err():
                    return self._fail(document_id, outbox_result.error)

                # Best effort inline projection để Neo4j có dữ liệu ngay.
                # Nếu lỗi, outbox vẫn giữ trạng thái retryable.
                event_id = outbox_result.unwrap()
                graph_result = self._graph_store.upsert_heading_graph(
                    document_id=document_id,
                    course_id=metadata.get("course_id"),
                    owner_id=metadata.get("owner_id"),
                    chunks=graph_chunks,
                )
                if graph_result.is_err():
                    logger.warning(
                        "process_document.graph_projection_failed",
                        document_id=document_id,
                        error=str(graph_result.error),
                    )
                    mark_failed = self._metadata_store.mark_outbox_failed(
                        event_id=event_id,
                        error_msg=str(graph_result.error),
                    )
                    if mark_failed.is_err():
                        logger.error(
                            "process_document.mark_outbox_failed_failed",
                            document_id=document_id,
                            event_id=event_id,
                            error=str(mark_failed.error),
                        )
                else:
                    mark_done = self._metadata_store.mark_outbox_done(event_id)
                    if mark_done.is_err():
                        logger.error(
                            "process_document.mark_outbox_done_failed",
                            document_id=document_id,
                            event_id=event_id,
                            error=str(mark_done.error),
                        )

                done_status = self._metadata_store.update_document_status(
                    document_id=document_id,
                    status=IngestionStatus.DONE,
                )
                if done_status.is_err():
                    return self._fail(document_id, done_status.error)

                duration_ms = (time.perf_counter() - started_at) * 1000
                span.set_attribute("chunks.count", len(chunks))
                span.set_attribute("duration_ms", duration_ms)

                logger.info(
                    "process_document.completed",
                    document_id=document_id,
                    chunks=len(chunks),
                    duration_ms=round(duration_ms, 2),
                )

                return Ok(
                    ProcessDocumentResponse(
                        document_id=document_id,
                        document_name=document.name,
                        doc_type=doc_type,
                        chunk_count=len(chunks),
                        chunks=[
                            ChunkSummary(
                                chunk_id=c.id,
                                heading_path=list(c.metadata.heading_path),
                                content_preview=c.content[:200],
                                content_length=len(c.content),
                                page_number=c.metadata.page_number,
                                has_images=len(c.images) > 0,
                            )
                            for c in chunks
                        ],
                        storage_key=storage_key,
                        processing_time_ms=round(duration_ms, 2),
                    )
                )
            except Exception as exc:
                logger.error(
                    "process_document.unhandled_exception",
                    document_id=document_id,
                    error=str(exc),
                )
                return self._fail(document_id, exc)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _detect_type(self, path: Path) -> DocumentType | None:
        return _EXT_MAP.get(path.suffix.lower())

    def _resolve_parser(self, doc_type: DocumentType) -> IParser | None:
        for parser in self._parsers:
            if parser.supports(doc_type):
                return parser
        return None

    def _align_parsed_document(
        self,
        parsed_doc: ParsedDocument,
        document: Document,
        language: str | None,
        metadata: dict[str, str],
    ) -> None:
        """
        Đồng bộ ParsedDocument với document context do use case quản lý.
        Tránh lệch document_id/name khi parser tự sinh Document riêng.
        """
        parsed_doc.document = document
        if language:
            parsed_doc.language = language
        if metadata:
            parsed_doc.metadata = {**parsed_doc.metadata, **metadata}

    def _normalize_metadata(self, raw_metadata: dict | None) -> dict[str, str]:
        if not raw_metadata:
            return {}

        normalized: dict[str, str] = {}
        for raw_key, raw_value in raw_metadata.items():
            if raw_key is None or raw_value is None:
                continue

            key_text = str(raw_key).strip()
            value_text = str(raw_value).strip()
            if not key_text or not value_text:
                continue

            alias_key = self._normalize_alias_key(key_text)
            canonical_key = _META_KEY_ALIASES.get(alias_key, key_text)
            normalized[canonical_key] = value_text

        return normalized

    def _normalize_alias_key(self, key: str) -> str:
        return "".join(ch for ch in key.lower() if ch.isalnum())

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
            )
            for chunk in chunks
        ]

    def _build_graph_chunks(self, chunks: list[Chunk]) -> list[GraphChunk]:
        return [
            GraphChunk(
                chunk_id=chunk.id,
                chunk_index=chunk.metadata.chunk_index,
                heading_path=chunk.metadata.heading_path,
            )
            for chunk in chunks
        ]

    def _fail(self, document_id: str, error: Exception) -> Result[ProcessDocumentResponse, Exception]:
        fail_status = self._metadata_store.update_document_status(
            document_id=document_id,
            status=IngestionStatus.ERROR,
            error_msg=str(error),
        )
        if fail_status.is_err():
            logger.error(
                "process_document.fail_status_update_failed",
                document_id=document_id,
                error=str(fail_status.error),
            )
        return Err(error)

    def _mime_type(self, doc_type: DocumentType) -> str:
        return {
            DocumentType.PDF: "application/pdf",
            DocumentType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            DocumentType.PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            DocumentType.MARKDOWN: "text/markdown",
        }.get(doc_type, "application/octet-stream")

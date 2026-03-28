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
from src.domain.entities.document import Document, DocumentType
from src.domain.ports.chunker import IChunker
from src.domain.ports.embedder import IEmbedder
from src.domain.ports.file_storage import IFileStorage
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


class ProcessDocumentUseCase:
    def __init__(
        self,
        parsers: list[IParser],
        chunker: IChunker,
        embedder: IEmbedder,
        vector_store: IVectorStore,
        file_storage: IFileStorage,
        preprocessors: list[IPreprocessor] | None = None,
    ) -> None:
        self._parsers = parsers
        self._chunker = chunker
        self._embedder = embedder
        self._vector_store = vector_store
        self._file_storage = file_storage
        self._preprocessors = preprocessors or []

    def execute(
        self, request: ProcessDocumentRequest
    ) -> Result[ProcessDocumentResponse, Exception]:
        started_at = time.perf_counter()
        document_id = request.document_id or str(uuid.uuid4())

        with tracer.start_as_current_span("process_document") as span:
            span.set_attribute("document.id", document_id)
            span.set_attribute("file.path", str(request.file_path))

            logger.info(
                "process_document.started",
                document_id=document_id,
                file=request.file_path.name,
            )

            # 1. Detect document type
            doc_type = request.doc_type or self._detect_type(request.file_path)
            if doc_type is None:
                return Err(UnsupportedFileTypeError(request.file_path.suffix))

            span.set_attribute("document.type", doc_type.value)

            # 2. Build Document entity
            document = Document(
                id=document_id,
                name=request.file_path.name,
                path=request.file_path,
                doc_type=doc_type,
                size_bytes=request.file_path.stat().st_size,
                mime_type=self._mime_type(doc_type),
            )

            # 3. Upload raw file to MinIO
            storage_key = f"{doc_type.value}/{document_id}/{document.name}"
            upload_result = self._file_storage.upload(request.file_path, storage_key)
            if isinstance(upload_result, Err):
                logger.error("process_document.upload_failed", document_id=document_id, error=str(upload_result.error))
                return upload_result

            logger.debug("process_document.uploaded", document_id=document_id, key=storage_key)

            # 4. Preprocessors (OCR, v.v.) — áp dụng tuần tự nếu cần
            file_path = request.file_path
            for preprocessor in self._preprocessors:
                if preprocessor.should_apply(file_path):
                    logger.debug("process_document.preprocessing", preprocessor=type(preprocessor).__name__)
                    file_path = preprocessor.process(file_path)

            # 5. Parse
            with tracer.start_as_current_span("parse"):
                parser = self._resolve_parser(doc_type)
                if parser is None:
                    return Err(UnsupportedFileTypeError(doc_type.value))

                parse_result = parser.parse(file_path)
                if parse_result.is_err():
                    logger.error("process_document.parse_failed", document_id=document_id, error=str(parse_result.error))
                    return parse_result

            parsed_doc = parse_result.unwrap()
            logger.debug(
                "process_document.parsed",
                document_id=document_id,
                sections=len(parsed_doc.sections),
                pages=parsed_doc.page_count,
            )

            # 6. Chunk
            with tracer.start_as_current_span("chunk"):
                chunk_result = self._chunker.chunk(parsed_doc)
                if chunk_result.is_err():
                    logger.error("process_document.chunk_failed", document_id=document_id, error=str(chunk_result.error))
                    return chunk_result

            chunks = chunk_result.unwrap()
            logger.debug("process_document.chunked", document_id=document_id, chunks=len(chunks))

            # 7. Embed
            with tracer.start_as_current_span("embed"):
                embed_result = self._embedder.embed_chunks(chunks)
                if embed_result.is_err():
                    logger.error("process_document.embed_failed", document_id=document_id, error=str(embed_result.error))
                    return embed_result

            embeddings = embed_result.unwrap()

            # 8. Upsert to vector store
            with tracer.start_as_current_span("upsert"):
                upsert_result = self._vector_store.upsert(chunks, embeddings)
                if upsert_result.is_err():
                    logger.error("process_document.upsert_failed", document_id=document_id, error=str(upsert_result.error))
                    return upsert_result

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

    def _mime_type(self, doc_type: DocumentType) -> str:
        return {
            DocumentType.PDF: "application/pdf",
            DocumentType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            DocumentType.PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            DocumentType.MARKDOWN: "text/markdown",
        }.get(doc_type, "application/octet-stream")

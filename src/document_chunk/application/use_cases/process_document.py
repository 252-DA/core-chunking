"""
ProcessDocumentUseCase — gRPC sync path: detect type → upload MinIO → run pipeline.

Flow:
  File → detect type → create Document → upsert record (QUEUED) → upload MinIO
       → PipelineCore (parse → chunk → embed → upsert + outbox)
       → ProcessDocumentResponse
"""
import time
import uuid
from pathlib import Path

from document_chunk.application.dto.document_dto import (
    ChunkSummary,
    ProcessDocumentRequest,
    ProcessDocumentResponse,
)
from document_chunk.application.use_cases._pipeline_core import PipelineCore
from document_chunk.domain.constants import EXT_MAP, MIME_MAP
from document_chunk.domain.entities.document import Document
from document_chunk.domain.exceptions import UnsupportedFileTypeError
from document_chunk.domain.ports.chunker import IChunker
from document_chunk.domain.ports.embedder import IEmbedder
from document_chunk.domain.ports.file_storage import IFileStorage
from document_chunk.domain.ports.job_queue import EnrichmentJobPayload, IJobQueue
from document_chunk.domain.ports.metadata_store import IMetadataStore, IngestionStatus
from document_chunk.domain.ports.parser import IParser
from document_chunk.domain.ports.preprocessor import IPreprocessor
from document_chunk.domain.ports.vector_store import IVectorStore
from document_chunk.shared.logger import get_logger
from document_chunk.shared.metrics import ACTIVE_REQUESTS, DOCUMENTS_PROCESSED
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_META_KEY_ALIASES: dict[str, str] = {
    "courseid": "course_id",
    "ownerid": "owner_id",
    "sourcefilename": "source_file_name",
    "filename": "source_file_name",
    "originalfilename": "source_file_name",
}


class ProcessDocumentUseCase:
    def __init__(
        self,
        parsers: list[IParser],
        chunker: IChunker,
        embedder: IEmbedder,
        vector_store: IVectorStore,
        file_storage: IFileStorage,
        metadata_store: IMetadataStore,
        job_queue: IJobQueue,
        preprocessors: list[IPreprocessor] | None = None,
    ) -> None:
        self._file_storage = file_storage
        self._metadata_store = metadata_store
        self._job_queue = job_queue
        self._pipeline = PipelineCore(
            parsers=parsers,
            chunker=chunker,
            embedder=embedder,
            vector_store=vector_store,
            metadata_store=metadata_store,
            preprocessors=preprocessors,
        )

    def execute(
        self, request: ProcessDocumentRequest
    ) -> Result[ProcessDocumentResponse, Exception]:
        ACTIVE_REQUESTS.inc()
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
            doc_type = request.doc_type or EXT_MAP.get(request.file_path.suffix.lower())
            if doc_type is None:
                ACTIVE_REQUESTS.dec()
                return Err(UnsupportedFileTypeError(request.file_path.suffix))

            span.set_attribute("document.type", doc_type.value)

            try:
                document = Document(
                    id=document_id,
                    name=source_file_name,
                    path=request.file_path,
                    doc_type=doc_type,
                    size_bytes=request.file_path.stat().st_size,
                    mime_type=MIME_MAP.get(doc_type, "application/octet-stream"),
                )
                storage_key = f"{doc_type.value}/{document_id}/{document.name}"
                metadata["storage_key"] = storage_key

                # 2. Upsert document record (QUEUED) — SQL source of truth
                store_result = self._metadata_store.upsert_document(
                    document=document,
                    metadata=metadata,
                    status=IngestionStatus.QUEUED,
                )
                if store_result.is_err():
                    ACTIVE_REQUESTS.dec()
                    return Err(store_result.error)

                # 3. Upload raw file to object storage
                upload_result = self._file_storage.upload(request.file_path, storage_key)
                if upload_result.is_err():
                    logger.error(
                        "process_document.upload_failed",
                        document_id=document_id,
                        error=str(upload_result.error),
                    )
                    ACTIVE_REQUESTS.dec()
                    DOCUMENTS_PROCESSED.labels(status="failed", doc_type=doc_type.value).inc()
                    self._metadata_store.update_document_status(
                        document_id, IngestionStatus.ERROR, error_msg=str(upload_result.error)
                    )
                    return Err(upload_result.error)

                logger.debug("process_document.uploaded", document_id=document_id, key=storage_key)

                # 4. Run core pipeline (parse → chunk → embed → upsert + outbox)
                core_result = self._pipeline.run(
                    document=document,
                    metadata=metadata,
                    language=metadata.get("language"),
                )

                ACTIVE_REQUESTS.dec()

                if core_result.is_err():
                    logger.error(
                        "process_document.pipeline_failed",
                        document_id=document_id,
                        error=str(core_result.error),
                    )
                    return Err(core_result.error)

                core = core_result.unwrap()
                chunks = core.chunks
                duration_ms = (time.perf_counter() - started_at) * 1000
                span.set_attribute("chunks.count", len(chunks))
                span.set_attribute("duration_ms", duration_ms)

                logger.info(
                    "process_document.completed",
                    document_id=document_id,
                    chunks=len(chunks),
                    duration_ms=round(duration_ms, 2),
                )
                self._enqueue_enrichment(document_id)

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
                ACTIVE_REQUESTS.dec()
                DOCUMENTS_PROCESSED.labels(status="failed", doc_type=doc_type.value).inc()
                self._metadata_store.update_document_status(
                    document_id, IngestionStatus.ERROR, error_msg=str(exc)
                )
                logger.error(
                    "process_document.unhandled_exception",
                    document_id=document_id,
                    error=str(exc),
                )
                return Err(exc)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

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
            alias_key = "".join(ch for ch in key_text.lower() if ch.isalnum())
            canonical_key = _META_KEY_ALIASES.get(alias_key, key_text)
            normalized[canonical_key] = value_text

        return normalized

    def _enqueue_enrichment(self, document_id: str) -> None:
        enqueue_result = self._job_queue.enqueue_enrichment(
            EnrichmentJobPayload(document_id=document_id)
        )
        if enqueue_result.is_err():
            logger.warning(
                "process_document.enrichment_enqueue_failed",
                document_id=document_id,
                error=str(enqueue_result.error),
            )

"""
EnqueueDocumentUseCase — async ingestion entry point.

Flow:
  file_path → upload MinIO → upsert document record (QUEUED) → enqueue job
            → EnqueueDocumentResponse (document_id, job_id)

The actual pipeline (parse → chunk → embed → upsert) runs in the BullMQ worker
via RunPipelineUseCase.
"""
import uuid
from dataclasses import dataclass
from pathlib import Path

from src.domain.constants import EXT_MAP, MIME_MAP
from src.domain.entities.document import Document
from src.domain.exceptions import UnsupportedFileTypeError
from src.domain.ports.file_storage import IFileStorage
from src.domain.ports.job_queue import DocumentJobPayload, IJobQueue
from src.domain.ports.metadata_store import IMetadataStore, IngestionStatus
from src.shared.logger import get_logger
from src.shared.result import Err, Ok, Result
from src.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)


@dataclass
class EnqueueDocumentRequest:
    file_path: Path
    file_name: str
    document_id: str | None = None
    language: str | None = None
    metadata: dict | None = None


@dataclass
class EnqueueDocumentResponse:
    document_id: str
    job_id: str
    status: str = "QUEUED"


class EnqueueDocumentUseCase:
    def __init__(
        self,
        file_storage: IFileStorage,
        metadata_store: IMetadataStore,
        job_queue: IJobQueue,
    ) -> None:
        self._file_storage = file_storage
        self._metadata_store = metadata_store
        self._job_queue = job_queue

    def execute(
        self, request: EnqueueDocumentRequest
    ) -> Result[EnqueueDocumentResponse, Exception]:
        with tracer.start_as_current_span("enqueue_document") as span:
            document_id = request.document_id or str(uuid.uuid4())
            suffix = Path(request.file_name).suffix.lower()
            doc_type = EXT_MAP.get(suffix)
            if doc_type is None:
                return Err(UnsupportedFileTypeError(suffix))

            storage_key = f"{doc_type.value}/{document_id}/{request.file_name}"
            metadata = dict(request.metadata or {})
            metadata["storage_key"] = storage_key
            if request.language and request.language.strip():
                metadata["language"] = request.language.strip()

            span.set_attribute("document.id", document_id)
            span.set_attribute("document.type", doc_type.value)

            logger.info(
                "enqueue_document.started",
                document_id=document_id,
                file_name=request.file_name,
            )

            # 1. Upload raw file to MinIO (staging before worker picks it up)
            upload_result = self._file_storage.upload(request.file_path, storage_key)
            if upload_result.is_err():
                logger.error(
                    "enqueue_document.upload_failed",
                    document_id=document_id,
                    error=str(upload_result.error),
                )
                return Err(upload_result.error)

            # 2. Persist document record with QUEUED status
            document = Document(
                id=document_id,
                name=request.file_name,
                path=request.file_path,
                doc_type=doc_type,
                size_bytes=request.file_path.stat().st_size,
                mime_type=MIME_MAP.get(doc_type, "application/octet-stream"),
            )
            upsert_result = self._metadata_store.upsert_document(
                document=document,
                metadata=metadata,
                status=IngestionStatus.QUEUED,
            )
            if upsert_result.is_err():
                logger.error(
                    "enqueue_document.metadata_failed",
                    document_id=document_id,
                    error=str(upsert_result.error),
                )
                return Err(upsert_result.error)

            # 3. Enqueue BullMQ job
            payload = DocumentJobPayload(
                document_id=document_id,
                storage_key=storage_key,
                file_name=request.file_name,
                language=request.language,
                metadata=metadata,
            )
            enqueue_result = self._job_queue.enqueue(payload)
            if enqueue_result.is_err():
                logger.error(
                    "enqueue_document.enqueue_failed",
                    document_id=document_id,
                    error=str(enqueue_result.error),
                )
                return Err(enqueue_result.error)

            job_id = enqueue_result.unwrap()
            logger.info("enqueue_document.queued", document_id=document_id, job_id=job_id)
            return Ok(EnqueueDocumentResponse(document_id=document_id, job_id=job_id))

"""
ChunkingServicer — gRPC method implementations.

Bridges gRPC ↔ application use cases.
No business logic here — only:
  request validation → DTO mapping → use_case.execute() → gRPC response mapping.

File transfer:
  Client sends file_data (bytes) in the request.
  Servicer writes to a temp file → passes path to use case → deletes temp file.
"""
import tempfile
from pathlib import Path

import grpc

from src.application.dto.document_dto import ProcessDocumentRequest
from src.application.dto.search_dto import SearchRequest
from src.application.use_cases.delete_document import (
    DeleteDocumentRequest,
    DeleteDocumentUseCase,
)
from src.application.use_cases.enqueue_document import (
    EnqueueDocumentRequest,
    EnqueueDocumentUseCase,
)
from src.application.use_cases.process_document import ProcessDocumentUseCase
from src.application.use_cases.search_chunks import SearchChunksUseCase
from src.delivery.grpc.proto import chunking_pb2, chunking_pb2_grpc
from src.domain.entities.document import DocumentType
from src.domain.ports.metadata_store import IMetadataStore
from src.shared.logger import get_logger
from src.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_VERSION = "1.0.0"


class ChunkingServicer(chunking_pb2_grpc.ChunkingServiceServicer):
    def __init__(
        self,
        process_use_case: ProcessDocumentUseCase,
        search_use_case: SearchChunksUseCase,
        enqueue_use_case: EnqueueDocumentUseCase,
        delete_use_case: DeleteDocumentUseCase,
        metadata_store: IMetadataStore,
    ) -> None:
        self._process = process_use_case
        self._search = search_use_case
        self._enqueue = enqueue_use_case
        self._delete = delete_use_case
        self._metadata_store = metadata_store

    # ------------------------------------------------------------------
    # ProcessDocument (sync)
    # ------------------------------------------------------------------

    def ProcessDocument(
        self,
        request: chunking_pb2.ProcessDocumentRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.ProcessDocumentResponse:
        with tracer.start_as_current_span("grpc.ProcessDocument"):
            if not request.file_data:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "file_data is required")
            if not request.file_name:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "file_name is required")

            logger.info(
                "grpc.ProcessDocument.received",
                file_name=request.file_name,
                size_bytes=len(request.file_data),
            )

            suffix = Path(request.file_name).suffix or ".tmp"
            tmp_path: Path | None = None

            try:
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(request.file_data)
                    tmp_path = Path(tmp.name)

                dto = ProcessDocumentRequest(
                    file_path=tmp_path,
                    document_id=request.document_id or None,
                    original_file_name=request.file_name or None,
                    language=request.language or None,
                    metadata=dict(request.metadata),
                )

                result = self._process.execute(dto)

                if result.is_err():
                    err_msg = str(result.error)
                    logger.error("grpc.ProcessDocument.failed", error=err_msg)
                    context.abort(grpc.StatusCode.INTERNAL, err_msg)

                resp = result.unwrap()
                return chunking_pb2.ProcessDocumentResponse(
                    document_id=resp.document_id,
                    document_name=resp.document_name,
                    doc_type=resp.doc_type.value,
                    chunk_count=resp.chunk_count,
                    chunks=[
                        chunking_pb2.ChunkSummary(
                            chunk_id=c.chunk_id,
                            heading_path=c.heading_path,
                            content_preview=c.content_preview,
                            content_length=c.content_length,
                            page_number=c.page_number or 0,
                            has_images=c.has_images,
                        )
                        for c in resp.chunks
                    ],
                    storage_key=resp.storage_key,
                    processing_time_ms=resp.processing_time_ms,
                )

            except Exception as e:
                logger.error("grpc.ProcessDocument.exception", error=str(e))
                context.abort(grpc.StatusCode.INTERNAL, str(e))

            finally:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def Search(
        self,
        request: chunking_pb2.SearchRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.SearchResponse:
        with tracer.start_as_current_span("grpc.Search"):
            if not request.query.strip():
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "query is required")

            logger.info("grpc.Search.received", query=request.query, top_k=request.top_k)

            try:
                dto = SearchRequest(
                    query=request.query,
                    top_k=request.top_k or 10,
                    score_threshold=request.score_threshold or 0.0,
                    doc_types=[DocumentType(dt) for dt in request.doc_types if dt],
                    document_ids=list(request.document_ids),
                    language=request.language or None,
                    course_id=request.course_id or None,
                    owner_id=request.owner_id or None,
                )

                result = self._search.execute(dto)

                if result.is_err():
                    err_msg = str(result.error)
                    logger.error("grpc.Search.failed", error=err_msg)
                    context.abort(grpc.StatusCode.INTERNAL, err_msg)

                resp = result.unwrap()
                return chunking_pb2.SearchResponse(
                    query=resp.query,
                    results=[
                        chunking_pb2.SearchResultItem(
                            chunk_id=item.chunk_id,
                            document_id=item.document_id,
                            document_name=item.document_name,
                            doc_type=item.doc_type.value,
                            heading_path=item.heading_path,
                            content=item.content,
                            score=item.score,
                            rank=item.rank,
                            page_number=item.page_number or 0,
                        )
                        for item in resp.results
                    ],
                    total_found=resp.total_found,
                    search_time_ms=resp.search_time_ms,
                )

            except Exception as e:
                logger.error("grpc.Search.exception", error=str(e))
                context.abort(grpc.StatusCode.INTERNAL, str(e))

    # ------------------------------------------------------------------
    # DeleteDocument
    # ------------------------------------------------------------------

    def DeleteDocument(
        self,
        request: chunking_pb2.DeleteDocumentRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.DeleteDocumentResponse:
        if not request.document_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "document_id is required")

        logger.info("grpc.DeleteDocument.received", document_id=request.document_id)

        result = self._delete.execute(
            DeleteDocumentRequest(document_id=request.document_id)
        )

        if result.is_err():
            return chunking_pb2.DeleteDocumentResponse(
                success=False,
                message=str(result.error),
            )

        logger.info("grpc.DeleteDocument.done", document_id=request.document_id)
        response = result.unwrap()
        return chunking_pb2.DeleteDocumentResponse(
            success=response.success,
            message=response.message,
        )

    # ------------------------------------------------------------------
    # EnqueueDocument (async)
    # ------------------------------------------------------------------

    def EnqueueDocument(
        self,
        request: chunking_pb2.ProcessDocumentRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.EnqueueDocumentResponse:
        with tracer.start_as_current_span("grpc.EnqueueDocument"):
            if not request.file_data:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "file_data is required")
            if not request.file_name:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "file_name is required")

            logger.info(
                "grpc.EnqueueDocument.received",
                file_name=request.file_name,
                size_bytes=len(request.file_data),
            )

            suffix = Path(request.file_name).suffix or ".tmp"
            tmp_path: Path | None = None

            try:
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(request.file_data)
                    tmp_path = Path(tmp.name)

                dto = EnqueueDocumentRequest(
                    file_path=tmp_path,
                    file_name=request.file_name,
                    document_id=request.document_id or None,
                    language=request.language or None,
                    metadata=dict(request.metadata),
                )

                result = self._enqueue.execute(dto)

                if result.is_err():
                    err_msg = str(result.error)
                    logger.error("grpc.EnqueueDocument.failed", error=err_msg)
                    context.abort(grpc.StatusCode.INTERNAL, err_msg)

                resp = result.unwrap()
                return chunking_pb2.EnqueueDocumentResponse(
                    document_id=resp.document_id,
                    status=resp.status,
                    job_id=resp.job_id,
                )

            except Exception as e:
                logger.error("grpc.EnqueueDocument.exception", error=str(e))
                context.abort(grpc.StatusCode.INTERNAL, str(e))

            finally:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink()

    # ------------------------------------------------------------------
    # GetDocumentStatus
    # ------------------------------------------------------------------

    def GetDocumentStatus(
        self,
        request: chunking_pb2.GetDocumentStatusRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.GetDocumentStatusResponse:
        if not request.document_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "document_id is required")

        result = self._metadata_store.get_document_status(request.document_id)

        if result.is_err():
            context.abort(grpc.StatusCode.INTERNAL, str(result.error))

        status_tuple = result.unwrap()
        if status_tuple is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Document {request.document_id} not found")

        status, error_msg, storage_key = status_tuple
        return chunking_pb2.GetDocumentStatusResponse(
            document_id=request.document_id,
            status=status.value,
            error_msg=error_msg or "",
            storage_key=storage_key or "",
        )

    # ------------------------------------------------------------------
    # HealthCheck
    # ------------------------------------------------------------------

    def HealthCheck(
        self,
        request: chunking_pb2.HealthCheckRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.HealthCheckResponse:
        return chunking_pb2.HealthCheckResponse(
            status="ok",
            version=_VERSION,
        )

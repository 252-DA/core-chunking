from dataclasses import dataclass

from document_chunk.domain.ports.metadata_store import IMetadataStore
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)


@dataclass(frozen=True)
class DeleteDocumentRequest:
    document_id: str


@dataclass(frozen=True)
class DeleteDocumentResponse:
    document_id: str
    success: bool
    message: str


class DeleteDocumentUseCase:
    """
    Delete a document from SQL source-of-truth and enqueue downstream cleanup.

    MetadataStore is responsible for appending the `DOCUMENT_DELETED` outbox event
    in the same transaction as the SQL delete.
    """

    def __init__(self, metadata_store: IMetadataStore) -> None:
        self._metadata_store = metadata_store

    def execute(
        self,
        request: DeleteDocumentRequest,
    ) -> Result[DeleteDocumentResponse, Exception]:
        document_id = request.document_id

        with tracer.start_as_current_span("delete_document") as span:
            span.set_attribute("document.id", document_id)
            logger.info("delete_document.started", document_id=document_id)

            metadata_result = self._metadata_store.delete(document_id)
            if metadata_result.is_err():
                logger.error(
                    "delete_document.metadata_delete_failed",
                    document_id=document_id,
                    error=str(metadata_result.error),
                )
                return Err(metadata_result.error)

            logger.info("delete_document.completed", document_id=document_id)
            return Ok(
                DeleteDocumentResponse(
                    document_id=document_id,
                    success=True,
                    message=f"Delete scheduled for document {document_id}",
                )
            )

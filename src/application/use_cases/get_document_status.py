from dataclasses import dataclass

from src.application.dto.generation_dto import DocumentStatusResponse
from src.domain.ports.metadata_store import IMetadataStore
from src.shared.result import Err, Ok, Result


@dataclass(frozen=True)
class GetDocumentStatusRequest:
    document_id: str


class GetDocumentStatusUseCase:
    def __init__(self, metadata_store: IMetadataStore) -> None:
        self._metadata_store = metadata_store

    def execute(
        self,
        request: GetDocumentStatusRequest,
    ) -> Result[DocumentStatusResponse | None, Exception]:
        result = self._metadata_store.get_document_status(request.document_id)
        if result.is_err():
            return Err(result.error)

        payload = result.unwrap()
        if payload is None:
            return Ok(None)

        status, error_msg, storage_key = payload
        return Ok(
            DocumentStatusResponse(
                document_id=request.document_id,
                status=status,
                error_msg=error_msg,
                storage_key=storage_key,
            )
        )

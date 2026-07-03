from document_chunk.application.use_cases.delete_document import (
    DeleteDocumentRequest,
    DeleteDocumentUseCase,
)
from document_chunk.shared.result import Err


class TestDeleteDocumentUseCase:
    def test_deletes_metadata_and_enqueues_cleanup(self, mock_metadata_store):
        use_case = DeleteDocumentUseCase(metadata_store=mock_metadata_store)

        result = use_case.execute(DeleteDocumentRequest(document_id="doc-001"))

        assert result.is_ok()
        mock_metadata_store.delete.assert_called_once_with("doc-001")
        assert result.unwrap().message == "Delete scheduled for document doc-001"

    def test_stops_when_metadata_delete_fails(self, mock_metadata_store):
        mock_metadata_store.delete.return_value = Err(RuntimeError("sql down"))
        use_case = DeleteDocumentUseCase(metadata_store=mock_metadata_store)

        result = use_case.execute(DeleteDocumentRequest(document_id="doc-001"))

        assert result.is_err()
        mock_metadata_store.delete.assert_called_once_with("doc-001")

from unittest.mock import MagicMock

from src.application.dto.search_dto import SearchResponse
from src.application.use_cases.delete_document import DeleteDocumentResponse
from src.delivery.grpc.proto import chunking_pb2
from src.delivery.grpc.servicer import ChunkingServicer
from src.domain.ports.metadata_store import IngestionStatus
from src.shared.result import Ok


class _FakeContext:
    def abort(self, code, message):
        raise AssertionError(f"unexpected grpc abort: {code} {message}")


class TestChunkingServicer:
    def test_search_maps_course_and_owner_filters(self):
        process_use_case = MagicMock()
        search_use_case = MagicMock()
        enqueue_use_case = MagicMock()
        delete_use_case = MagicMock()
        metadata_store = MagicMock()
        search_use_case.execute.return_value = Ok(
            SearchResponse(
                query="calculus",
                results=[],
                total_found=0,
                search_time_ms=1.23,
            )
        )
        servicer = ChunkingServicer(
            process_use_case=process_use_case,
            search_use_case=search_use_case,
            enqueue_use_case=enqueue_use_case,
            delete_use_case=delete_use_case,
            metadata_store=metadata_store,
        )

        response = servicer.Search(
            chunking_pb2.SearchRequest(
                query="calculus",
                top_k=5,
                course_id="course-001",
                owner_id="owner-001",
            ),
            _FakeContext(),
        )

        dto = search_use_case.execute.call_args.args[0]
        assert dto.course_id == "course-001"
        assert dto.owner_id == "owner-001"
        assert response.query == "calculus"
        assert response.total_found == 0

    def test_get_document_status_includes_storage_key(self):
        process_use_case = MagicMock()
        search_use_case = MagicMock()
        enqueue_use_case = MagicMock()
        delete_use_case = MagicMock()
        metadata_store = MagicMock()
        metadata_store.get_document_status.return_value = Ok(
            (IngestionStatus.DONE, None, "pdf/doc-001/test.pdf")
        )
        servicer = ChunkingServicer(
            process_use_case=process_use_case,
            search_use_case=search_use_case,
            enqueue_use_case=enqueue_use_case,
            delete_use_case=delete_use_case,
            metadata_store=metadata_store,
        )

        response = servicer.GetDocumentStatus(
            chunking_pb2.GetDocumentStatusRequest(document_id="doc-001"),
            _FakeContext(),
        )

        assert response.document_id == "doc-001"
        assert response.status == IngestionStatus.DONE.value
        assert response.storage_key == "pdf/doc-001/test.pdf"

    def test_delete_document_uses_delete_use_case(self):
        process_use_case = MagicMock()
        search_use_case = MagicMock()
        enqueue_use_case = MagicMock()
        delete_use_case = MagicMock()
        metadata_store = MagicMock()
        delete_use_case.execute.return_value = Ok(
            DeleteDocumentResponse(
                document_id="doc-001",
                success=True,
                message="Delete scheduled for document doc-001",
            )
        )
        servicer = ChunkingServicer(
            process_use_case=process_use_case,
            search_use_case=search_use_case,
            enqueue_use_case=enqueue_use_case,
            delete_use_case=delete_use_case,
            metadata_store=metadata_store,
        )

        response = servicer.DeleteDocument(
            chunking_pb2.DeleteDocumentRequest(document_id="doc-001"),
            _FakeContext(),
        )

        assert response.success is True
        assert response.message == "Delete scheduled for document doc-001"
        delete_use_case.execute.assert_called_once()

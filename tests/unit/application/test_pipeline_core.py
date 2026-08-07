from dataclasses import replace

from document_chunk.application.use_cases._pipeline_core import PipelineCore
from document_chunk.domain.exceptions import DocumentStaleError, ProcessingError
from document_chunk.domain.ports.metadata_store import IngestionStatus
from document_chunk.shared.result import Err


class TestPipelineCore:
    def test_fails_fast_when_document_path_is_missing(
        self,
        sample_document,
        mock_parser,
        mock_chunker,
        mock_embedder,
        mock_vector_store,
        mock_metadata_store,
    ):
        pipeline = PipelineCore(
            parsers=[mock_parser],
            chunker=mock_chunker,
            embedder=mock_embedder,
            vector_store=mock_vector_store,
            metadata_store=mock_metadata_store,
        )

        document = replace(sample_document, path=None)
        result = pipeline.run(document=document, metadata={})

        assert result.is_err()
        assert isinstance(result.error, ProcessingError)
        assert str(result.error) == "PipelineCore requires document.path for local file access"
        mock_metadata_store.update_document_status.assert_called_once_with(
            document_id=sample_document.id,
            status=IngestionStatus.ERROR,
            error_msg="PipelineCore requires document.path for local file access",
        )
        mock_parser.parse.assert_not_called()
        mock_vector_store.upsert.assert_not_called()
        mock_metadata_store.upsert_chunks_with_outbox.assert_not_called()

    def test_stops_immediately_when_document_deleted_during_pipeline(
        self,
        sample_document,
        mock_parser,
        mock_chunker,
        mock_embedder,
        mock_vector_store,
        mock_metadata_store,
    ):
        # Document bị soft-delete trước khi pipeline kịp ghi → status update
        # báo stale → pipeline dừng ngay, không ghi Qdrant/chunk/outbox.
        mock_metadata_store.update_document_status.return_value = Err(
            DocumentStaleError("document doc-001 not found or deleted", document_id="doc-001")
        )

        pipeline = PipelineCore(
            parsers=[mock_parser],
            chunker=mock_chunker,
            embedder=mock_embedder,
            vector_store=mock_vector_store,
            metadata_store=mock_metadata_store,
        )

        result = pipeline.run(document=sample_document, metadata={})

        assert result.is_err()
        assert isinstance(result.error, DocumentStaleError)
        mock_parser.parse.assert_not_called()
        mock_chunker.chunk.assert_not_called()
        mock_vector_store.upsert.assert_not_called()
        mock_metadata_store.upsert_chunks_with_outbox.assert_not_called()

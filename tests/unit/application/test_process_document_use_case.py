from document_chunk.application.dto.document_dto import ProcessDocumentRequest
from document_chunk.application.use_cases.process_document import ProcessDocumentUseCase
from document_chunk.domain.outbox_events import OutboxEventType
from document_chunk.shared.result import Err


class TestProcessDocumentUseCase:
    def test_enqueues_enrichment_after_core_success(
        self,
        sample_document,
        mock_parser,
        mock_chunker,
        mock_embedder,
        mock_vector_store,
        mock_file_storage,
        mock_metadata_store,
        mock_job_queue,
    ):
        use_case = ProcessDocumentUseCase(
            parsers=[mock_parser],
            chunker=mock_chunker,
            embedder=mock_embedder,
            vector_store=mock_vector_store,
            file_storage=mock_file_storage,
            metadata_store=mock_metadata_store,
            job_queue=mock_job_queue,
        )

        result = use_case.execute(
            ProcessDocumentRequest(
                file_path=sample_document.path,
                document_id=sample_document.id,
                original_file_name=sample_document.name,
                language="en",
                metadata={"course_id": "course-001", "owner_id": "owner-001"},
            )
        )

        assert result.is_ok()
        mock_job_queue.enqueue_enrichment.assert_called_once()
        payload = mock_job_queue.enqueue_enrichment.call_args.args[0]
        assert payload.document_id == sample_document.id
        mock_metadata_store.upsert_chunks_with_outbox.assert_called_once()
        _, outbox_kwargs = mock_metadata_store.upsert_chunks_with_outbox.call_args
        assert outbox_kwargs["event_type"] is OutboxEventType.HEADING_GRAPH_PROJECT
        assert outbox_kwargs["aggregate_id"] == sample_document.id
        assert outbox_kwargs["payload"]["document_name"] == sample_document.name
        assert outbox_kwargs["payload"]["doc_type"] == sample_document.doc_type.value
        assert outbox_kwargs["payload"]["chunks"] == [
            {
                "chunk_id": "chunk-001",
                "chunk_index": 0,
                "heading_path": ["Introduction"],
                "page_number": 1,
                "language": "en",
            }
        ]
        mock_metadata_store.upsert_chunks.assert_not_called()
        mock_metadata_store.append_outbox_event.assert_not_called()

    def test_enrichment_enqueue_failure_does_not_fail_processing(
        self,
        sample_document,
        mock_parser,
        mock_chunker,
        mock_embedder,
        mock_vector_store,
        mock_file_storage,
        mock_metadata_store,
        mock_job_queue,
    ):
        mock_job_queue.enqueue_enrichment.return_value = Err(RuntimeError("redis down"))
        use_case = ProcessDocumentUseCase(
            parsers=[mock_parser],
            chunker=mock_chunker,
            embedder=mock_embedder,
            vector_store=mock_vector_store,
            file_storage=mock_file_storage,
            metadata_store=mock_metadata_store,
            job_queue=mock_job_queue,
        )

        result = use_case.execute(
            ProcessDocumentRequest(
                file_path=sample_document.path,
                document_id=sample_document.id,
                original_file_name=sample_document.name,
            )
        )

        assert result.is_ok()

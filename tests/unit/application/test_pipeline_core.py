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


def test_structural_retry_ids_and_cleanup_after_commit(
    sample_document, mock_parser, mock_embedder, mock_vector_store, mock_metadata_store, tmp_path,
):
    from unittest.mock import Mock
    from document_chunk.shared.result import Ok
    from document_chunk.adapters.chunkers.structural import StructuralChunker
    from document_chunk.adapters.chunkers.structural.tokens import TokenCounter
    from document_chunk.infrastructure.config import ChunkerConfig
    from document_chunk.domain.entities.document import ParsedDocument, Section, ElementType
    from tokenizers import Tokenizer, models, pre_tokenizers
    tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, 'body': 1}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    path = tmp_path/'tokenizer.json'
    tokenizer.save(str(path))
    config = ChunkerConfig(strategy='structural', tokenizer_path=str(path))
    chunker = StructuralChunker(config)
    mock_parser.parse.return_value = Ok(ParsedDocument(sample_document, [Section('body', ElementType.PARAGRAPH)], 1))
    mock_parser.supports.return_value = True
    mock_vector_store.delete_stale = Mock(return_value=Ok(None))
    events = []
    mock_vector_store.upsert.side_effect = lambda *a: events.append('upsert') or Ok(None)
    attempts = []
    def persist(**kwargs):
        attempts.append(kwargs['chunks'])
        events.append('persist')
        return Err(RuntimeError('temporary failure')) if len(attempts) == 1 else Ok('event')
    mock_metadata_store.upsert_chunks_with_outbox.side_effect = persist
    mock_vector_store.delete_stale.side_effect = lambda *a: events.append('cleanup') or Ok(None)
    core = PipelineCore([mock_parser], chunker, mock_embedder, mock_vector_store, mock_metadata_store)
    assert core.run(sample_document, {}).is_err()
    assert core.run(sample_document, {}).is_ok()
    assert attempts[0][0].chunk_id == attempts[1][0].chunk_id
    assert events == ['upsert', 'persist', 'upsert', 'persist', 'cleanup']

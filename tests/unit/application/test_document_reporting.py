"""
documents là bảng của core-api: PipelineCore và MapChunksToLosUseCase báo trạng
thái/chương qua port (worker cài bằng kết quả gửi core-api).
"""
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from document_chunk.adapters.metadata.postgres_metadata_store import PostgresMetadataStore
from document_chunk.application.use_cases._pipeline_core import PipelineCore
from document_chunk.application.use_cases.map_chunks_to_los import (
    MapChunksToLosRequest,
    MapChunksToLosUseCase,
)
from document_chunk.domain.exceptions import DocumentStaleError, MetadataStoreError
from document_chunk.domain.outbox_events import OutboxEventType
from document_chunk.domain.ports.chapter_matcher import ChapterMatch
from document_chunk.domain.ports.document_reporter import (
    IDocumentChapterReporter,
    IDocumentStatusReporter,
)
from document_chunk.domain.ports.job_queue import DocumentJobPayload, EnrichmentJobPayload
from document_chunk.domain.ports.metadata_store import (
    IngestionStatus,
    StoredChunkConcept,
    StoredConcept,
)
from document_chunk.infrastructure.config import SqlConfig
from document_chunk.shared.result import Err, Ok


class _Statuses(IDocumentStatusReporter):
    def __init__(self, fail_on: IngestionStatus | None = None, error: Exception | None = None):
        self.calls: list[tuple[str, IngestionStatus, str | None]] = []
        self._fail_on, self._error = fail_on, error

    def report_status(self, document_id, status, error_msg=None):
        self.calls.append((document_id, status, error_msg))
        if status == self._fail_on:
            return Err(self._error)
        return Ok(None)


class _Chapters(IDocumentChapterReporter):
    def __init__(self, result=None):
        self.calls = []
        self._result = result or Ok(None)

    def report_chapter(self, document_id, chapter_code, provenance, confidence, reason):
        self.calls.append((document_id, chapter_code, provenance, confidence, reason))
        return self._result


def _pipeline(mock_parser, mock_chunker, mock_embedder, mock_vector_store, mock_metadata_store):
    return PipelineCore(
        parsers=[mock_parser],
        chunker=mock_chunker,
        embedder=mock_embedder,
        vector_store=mock_vector_store,
        metadata_store=mock_metadata_store,
    )


class TestPipelineCoreStatusReporter:
    def test_every_transition_goes_to_the_reporter(
        self, sample_document, mock_parser, mock_chunker, mock_embedder,
        mock_vector_store, mock_metadata_store,
    ):
        reporter = _Statuses()

        result = _pipeline(
            mock_parser, mock_chunker, mock_embedder, mock_vector_store, mock_metadata_store
        ).run(document=sample_document, metadata={}, status_reporter=reporter)

        assert result.is_ok(), str(result.error)
        assert [status for _, status, _ in reporter.calls] == [
            IngestionStatus.PARSING,
            IngestionStatus.CHUNKING,
            IngestionStatus.EMBEDDING,
            IngestionStatus.INDEXED,
        ]
        mock_metadata_store.update_document_status.assert_not_called()
        mock_metadata_store.upsert_chunks_with_outbox.assert_called_once()

    def test_failure_is_reported_as_error(
        self, sample_document, mock_parser, mock_chunker, mock_embedder,
        mock_vector_store, mock_metadata_store,
    ):
        mock_embedder.embed_chunks.return_value = Err(RuntimeError("embedder down"))
        reporter = _Statuses()

        result = _pipeline(
            mock_parser, mock_chunker, mock_embedder, mock_vector_store, mock_metadata_store
        ).run(document=sample_document, metadata={}, status_reporter=reporter)

        assert result.is_err()
        assert reporter.calls[-1] == (sample_document.id, IngestionStatus.ERROR, "embedder down")
        mock_metadata_store.update_document_status.assert_not_called()

    def test_stale_document_stops_without_reporting_error(
        self, sample_document, mock_parser, mock_chunker, mock_embedder,
        mock_vector_store, mock_metadata_store,
    ):
        stale = DocumentStaleError("gone", document_id=sample_document.id)
        reporter = _Statuses(fail_on=IngestionStatus.PARSING, error=stale)

        result = _pipeline(
            mock_parser, mock_chunker, mock_embedder, mock_vector_store, mock_metadata_store
        ).run(document=sample_document, metadata={}, status_reporter=reporter)

        assert result.error is stale
        assert [status for _, status, _ in reporter.calls] == [IngestionStatus.PARSING]
        mock_parser.parse.assert_not_called()
        mock_vector_store.upsert.assert_not_called()
        mock_metadata_store.upsert_chunks_with_outbox.assert_not_called()

    def test_report_failure_fails_the_run(
        self, sample_document, mock_parser, mock_chunker, mock_embedder,
        mock_vector_store, mock_metadata_store,
    ):
        """Không báo được INDEXED (Redis) thì lượt xử lý lỗi để job chạy lại."""
        failure = ConnectionError("redis down")
        reporter = _Statuses(fail_on=IngestionStatus.INDEXED, error=failure)

        result = _pipeline(
            mock_parser, mock_chunker, mock_embedder, mock_vector_store, mock_metadata_store
        ).run(document=sample_document, metadata={}, status_reporter=reporter)

        assert result.error is failure


def _map_use_case(store, reporter):
    matcher = MagicMock()
    matcher.match.return_value = Ok(
        [ChapterMatch(chapter_code="2", best_code="2", runner_up_code="1", score=0.8765, margin=0.2)]
    )
    mapper = MagicMock()
    mapper.map.return_value = Ok([])
    return MapChunksToLosUseCase(
        metadata_store=store,
        graph_store=MagicMock(),
        lo_mapper=mapper,
        chapter_matcher=matcher,
        chapter_reporter=reporter,
    )


@pytest.fixture
def map_store(sample_stored_chunk_metadata):
    from document_chunk.domain.ports.metadata_store import (
        StoredChapter,
        StoredChapterLOLink,
        StoredCourse,
        StoredDocumentPlacement,
        StoredLearningOutcome,
    )

    store = MagicMock()
    store.get_curriculum.return_value = Ok(
        (
            StoredCourse(course_id="c", code="CO1", title_vi="Môn", title_en=None),
            [
                StoredChapter(chapter_id="ch1", course_id="c", code="1", title="Một", order_index=1),
                StoredChapter(chapter_id="ch2", course_id="c", code="2", title="Hai", order_index=2),
            ],
            [
                StoredLearningOutcome(lo_id="lo1", course_id="c", code="L.O.1", parent_code=None, statement_vi="a"),
                StoredLearningOutcome(lo_id="lo2", course_id="c", code="L.O.2", parent_code=None, statement_vi="b"),
            ],
            [],
        )
    )
    store.list_chapter_lo_links.return_value = Ok(
        [
            StoredChapterLOLink(chapter_id="ch1", lo_id="lo1"),
            StoredChapterLOLink(chapter_id="ch2", lo_id="lo2"),
        ]
    )
    store.list_chunks.return_value = Ok([sample_stored_chunk_metadata])
    store.get_document_placement.return_value = Ok(
        StoredDocumentPlacement(document_id="d", course_id="c", role="lecture")
    )
    store.delete_inferred_chunk_lo_mappings.return_value = Ok(0)
    return store


class TestMapChunksChapterReporter:
    def test_inferred_chapter_goes_to_the_reporter(self, map_store):
        reporter = _Chapters()

        result = _map_use_case(map_store, reporter).execute(
            MapChunksToLosRequest(document_id="d", course_id="c")
        )

        assert result.is_ok(), str(result.error)
        [(document_id, code, provenance, confidence, reason)] = reporter.calls
        assert (document_id, code, provenance) == ("d", "2", "content")
        assert confidence == pytest.approx(0.8765)
        assert "chương 2" in reason
        map_store.set_document_chapter.assert_not_called()

    def test_report_failure_does_not_block_the_mapping(self, map_store):
        """Báo chương lỗi (Redis) chỉ cảnh báo: chunk vẫn được map theo chương vừa suy ra."""
        from document_chunk.domain.ports.chunk_lo_mapper import ChunkLOMapping

        map_store.upsert_chunk_lo_mappings.return_value = Ok(1)
        map_store.delete_inferred_chunk_lo_mappings.return_value = Ok(0)
        use_case = _map_use_case(map_store, _Chapters(Err(ConnectionError("redis down"))))
        chunk_id = map_store.list_chunks.return_value.unwrap()[0].chunk_id
        use_case._lo_mapper.map.return_value = Ok(
            [ChunkLOMapping(chunk_id=chunk_id, lo_id="lo2", confidence=0.9, source="heading")]
        )
        use_case._graph_store.upsert_chunk_lo_mappings.return_value = Ok(None)

        result = use_case.execute(MapChunksToLosRequest(document_id="d", course_id="c"))

        assert result.is_ok(), str(result.error)
        assert result.unwrap().mapping_count == 1
        # Mapper vẫn nhận chương suy ra dù báo không đi được.
        hints = use_case._lo_mapper.map.call_args.args[2]
        assert hints.document_chapter == "2"
        map_store.delete_inferred_chunk_lo_mappings.assert_called_once()
        map_store.upsert_chunk_lo_mappings.assert_called_once()

    def test_known_chapter_is_not_reported(self, map_store):
        from document_chunk.domain.ports.metadata_store import StoredDocumentPlacement

        map_store.get_document_placement.return_value = Ok(
            StoredDocumentPlacement(
                document_id="d", course_id="c", role="lecture",
                chapter_code="1", chapter_provenance="confirmed",
            )
        )
        reporter = _Chapters()

        _map_use_case(map_store, reporter).execute(MapChunksToLosRequest(document_id="d", course_id="c"))

        assert reporter.calls == []

    def test_without_reporter_skips_reporting_without_writing_documents(self, map_store):
        use_case = _map_use_case(map_store, None)

        result = use_case.execute(MapChunksToLosRequest(document_id="d", course_id="c"))

        assert result.is_ok()
        map_store.set_document_chapter.assert_not_called()


class TestJobPayloadGeneration:
    def test_defaults_to_zero_for_old_jobs(self):
        assert DocumentJobPayload(document_id="d", storage_key="k", file_name="f").processing_generation == 0
        assert EnrichmentJobPayload(document_id="d").processing_generation == 0

    @pytest.mark.parametrize("value", [-1, "1", 1.0, True, None])
    def test_rejects_non_integer_generation(self, value):
        with pytest.raises(ValueError):
            DocumentJobPayload(document_id="d", storage_key="k", file_name="f", processing_generation=value)
        with pytest.raises(ValueError):
            EnrichmentJobPayload(document_id="d", processing_generation=value)


class _FakeCursor:
    def __init__(self, exists=True):
        self.execute_calls = []
        self.executemany_calls = []
        self._exists = exists

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params):
        self.execute_calls.append((query, params))

    def executemany(self, query, rows):
        self.executemany_calls.append((query, rows))

    def fetchone(self):
        return (self._exists,)


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commit_calls = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commit_calls += 1


def _store_with(monkeypatch, cursor):
    store = PostgresMetadataStore(SqlConfig(enabled=True))
    connection = _FakeConnection(cursor)

    @contextmanager
    def fake_connection():
        yield connection

    monkeypatch.setattr(store, "_connection", fake_connection)
    return store, connection


class TestPersistConceptEnrichment:
    def test_ai_rows_and_outbox_share_one_commit_and_never_touch_content_tables(self, monkeypatch):
        cursor = _FakeCursor()
        store, connection = _store_with(monkeypatch, cursor)

        result = store.persist_concept_enrichment(
            document_id="doc-001",
            concepts=[StoredConcept(concept_id="dao-ham", name="Đạo hàm", canonical_name="đạo hàm", slug="dao-ham")],
            chunk_concepts=[StoredChunkConcept(chunk_id="chunk-001", concept_id="dao-ham")],
            outbox_event_type=OutboxEventType.CONCEPT_GRAPH_PROJECT,
            outbox_payload={"document_id": "doc-001"},
        )

        assert result.is_ok()
        assert result.unwrap() is not None
        assert connection.commit_calls == 1
        sql = "\n".join(q for q, _ in cursor.execute_calls) + "\n".join(
            q for q, _ in cursor.executemany_calls
        )
        assert "SELECT EXISTS" in cursor.execute_calls[0][0]
        assert "DELETE FROM chunk_concepts" in sql
        assert "INSERT INTO concepts" in sql
        assert "INSERT INTO chunk_concepts" in sql
        assert "INSERT INTO outbox_events" in sql
        for table in ("lesson_cards", "quiz_items", "lessons", "documents SET"):
            assert table not in sql.replace("FROM documents", "")

    def test_without_event_returns_none(self, monkeypatch):
        cursor = _FakeCursor()
        store, connection = _store_with(monkeypatch, cursor)

        result = store.persist_concept_enrichment(
            document_id="doc-001", concepts=[], chunk_concepts=[]
        )

        assert result.unwrap() is None
        assert connection.commit_calls == 1
        assert not any("outbox_events" in q for q, _ in cursor.execute_calls)

    def test_deleted_document_writes_nothing(self, monkeypatch):
        cursor = _FakeCursor(exists=False)
        store, connection = _store_with(monkeypatch, cursor)

        result = store.persist_concept_enrichment(
            document_id="doc-001",
            concepts=[],
            chunk_concepts=[StoredChunkConcept(chunk_id="chunk-001", concept_id="x")],
            outbox_event_type=OutboxEventType.CONCEPT_GRAPH_PROJECT,
            outbox_payload={"document_id": "doc-001"},
        )

        assert isinstance(result.error, DocumentStaleError)
        assert connection.commit_calls == 0
        assert len(cursor.execute_calls) == 1
        assert cursor.executemany_calls == []

    def test_event_type_without_payload_is_rejected(self, monkeypatch):
        cursor = _FakeCursor()
        store, _ = _store_with(monkeypatch, cursor)

        result = store.persist_concept_enrichment(
            document_id="doc-001",
            concepts=[],
            chunk_concepts=[],
            outbox_event_type=OutboxEventType.CONCEPT_GRAPH_PROJECT,
        )

        assert isinstance(result.error, MetadataStoreError)
        assert cursor.execute_calls == []

from contextlib import contextmanager
from inspect import getsource

from document_chunk.adapters.metadata.postgres_metadata_store import PostgresMetadataStore
from document_chunk.domain.outbox_events import OutboxEventType
from document_chunk.domain.ports.metadata_store import (
    IngestionStatus,
    StoredChunkMetadata,
    StoredLessonCard,
    StoredQuizItem,
)
from document_chunk.infrastructure.config import SqlConfig


class _FakeCursor:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list[tuple]]] = []
        self.fetchone_result = None
        self.fetchall_result = []
        self.rowcount = 1  # UPDATE/INSERT mặc định ảnh hưởng 1 row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, query: str, params: tuple) -> None:
        self.execute_calls.append((query, params))

    def executemany(self, query: str, rows: list[tuple]) -> None:
        self.executemany_calls.append((query, rows))

    def fetchone(self):
        return self.fetchone_result

    def fetchall(self):
        return self.fetchall_result


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.commit_calls = 0

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commit_calls += 1


class TestPostgresMetadataStore:
    def test_store_facade_does_not_bootstrap_or_migrate_the_schema(self):
        runtime_sources = "\n".join(
            getsource(component)
            for component in PostgresMetadataStore.__mro__
            if component.__module__.startswith(
                "document_chunk.adapters.metadata.postgres_"
            )
        )

        assert not hasattr(PostgresMetadataStore, "_ensure_schema")
        assert not hasattr(PostgresMetadataStore, "_ensure_schema_initialized")
        assert "CREATE TABLE" not in runtime_sources
        assert "_schema_initialized" not in runtime_sources

    def test_upsert_chunks_with_outbox_uses_one_commit(self, monkeypatch):
        store = PostgresMetadataStore(SqlConfig(enabled=True))
        cursor = _FakeCursor()
        cursor.fetchone_result = (True,)  # document active
        connection = _FakeConnection(cursor)

        @contextmanager
        def fake_connection():
            yield connection

        monkeypatch.setattr(store, "_connection", fake_connection)

        result = store.upsert_chunks_with_outbox(
            chunks=[
                StoredChunkMetadata(
                    chunk_id="chunk-001",
                    document_id="doc-001",
                    chunk_index=0,
                    heading_path=("Introduction",),
                    heading_level=1,
                    page_number=1,
                    content_length=42,
                    language="en",
                )
            ],
            event_type=OutboxEventType.HEADING_GRAPH_PROJECT,
            aggregate_id="doc-001",
            payload={"document_id": "doc-001"},
        )

        assert result.is_ok()
        assert connection.commit_calls == 1
        assert len(cursor.executemany_calls) == 1
        assert "INSERT INTO chunks" in cursor.executemany_calls[0][0]
        assert cursor.executemany_calls[0][1] == [
            (
                "chunk-001",
                "doc-001",
                "",
                ["Introduction"],
                1,
                0,
                "en",
                "doc-001",
            )
        ]
        assert len(cursor.execute_calls) == 2
        assert "SELECT EXISTS" in cursor.execute_calls[0][0]
        assert "INSERT INTO outbox_events" in cursor.execute_calls[1][0]

    def test_persist_enrichment_batch_replaces_generated_rows_and_uses_one_commit(
        self, monkeypatch
    ):
        store = PostgresMetadataStore(SqlConfig(enabled=True))
        cursor = _FakeCursor()
        connection = _FakeConnection(cursor)

        @contextmanager
        def fake_connection():
            yield connection

        monkeypatch.setattr(store, "_connection", fake_connection)

        result = store.persist_enrichment_batch(
            document_id="doc-001",
            lesson_cards=[
                StoredLessonCard(
                    card_id="card-001",
                    document_id="doc-001",
                    primary_chunk_id="chunk-001",
                    source_chunk_ids=("chunk-001", "chunk-002"),
                    heading_path=("Chapter 1", "Matrices"),
                    title="Matrices",
                    bullets=("Rectangular arrays",),
                    key_insight="Matrices encode linear structure.",
                    card_index=0,
                    model_id="gemini-test",
                )
            ],
            quiz_items=[
                StoredQuizItem(
                    question_id="quiz-001",
                    document_id="doc-001",
                    primary_chunk_id="chunk-001",
                    source_chunk_ids=("chunk-001", "chunk-002"),
                    heading_path=("Chapter 1", "Matrices"),
                    question="What is a matrix?",
                    choices=("A set", "A rectangular array", "A scalar", "A graph"),
                    correct_index=1,
                    explanation="That is the standard definition.",
                    difficulty="easy",
                    question_index=0,
                    model_id="gemini-test",
                )
            ],
            concepts=[],
            chunk_concepts=[],
            outbox_event_type=OutboxEventType.CONCEPT_GRAPH_PROJECT,
            outbox_payload={"document_id": "doc-001"},
        )

        assert result.is_ok()
        assert connection.commit_calls == 1
        execute_sql = "\n".join(query for query, _ in cursor.execute_calls)
        executemany_sql = "\n".join(query for query, _ in cursor.executemany_calls)
        assert "UPDATE lesson_cards" in execute_sql
        assert "UPDATE quiz_items" in execute_sql
        assert "DELETE FROM chunk_concepts" in execute_sql
        assert "DELETE FROM outbox_events" in execute_sql
        assert executemany_sql.count("INSERT INTO lesson_cards") == 1
        assert executemany_sql.count("INSERT INTO quiz_items") == 1

    def test_list_chunks_reads_current_chunks_table(self, monkeypatch):
        store = PostgresMetadataStore(SqlConfig(enabled=True))
        cursor = _FakeCursor()
        cursor.fetchall_result = [
            (
                "chunk-001",
                "doc-001",
                0,
                ["Introduction"],
                1,
                1,
                42,
                "en",
                "Chunk content",
                "Introduction\n\nChunk content",
            )
        ]
        connection = _FakeConnection(cursor)

        @contextmanager
        def fake_connection():
            yield connection

        monkeypatch.setattr(store, "_connection", fake_connection)

        result = store.list_chunks("doc-001")

        assert result.is_ok()
        chunk = result.unwrap()[0]
        assert chunk.content_text == "Chunk content"
        assert chunk.embedding_input == "Introduction\n\nChunk content"
        assert "FROM chunks" in cursor.execute_calls[0][0]
        assert "chunk_contents" not in cursor.execute_calls[0][0]

    def test_update_document_status_returns_stale_when_rowcount_zero(self, monkeypatch):
        store = PostgresMetadataStore(SqlConfig(enabled=True))
        cursor = _FakeCursor()
        cursor.rowcount = 0  # document missing hoặc soft-deleted
        connection = _FakeConnection(cursor)

        @contextmanager
        def fake_connection():
            yield connection

        monkeypatch.setattr(store, "_connection", fake_connection)

        result = store.update_document_status("doc-001", IngestionStatus.PARSING)

        assert result.is_err()
        from document_chunk.domain.exceptions import DocumentStaleError

        assert isinstance(result.error, DocumentStaleError)
        assert connection.commit_calls == 0

    def test_update_document_status_never_resurrects_deleted_documents(self, monkeypatch):
        store = PostgresMetadataStore(SqlConfig(enabled=True))
        cursor = _FakeCursor()
        connection = _FakeConnection(cursor)

        @contextmanager
        def fake_connection():
            yield connection

        monkeypatch.setattr(store, "_connection", fake_connection)

        result = store.update_document_status("doc-001", IngestionStatus.ERROR, error_msg="boom")

        assert result.is_ok()
        assert connection.commit_calls == 1
        query = cursor.execute_calls[0][0]
        # Status update must not clear deleted_at (would resurrect soft-deleted docs)
        # and must only touch non-deleted rows.
        assert "deleted_at = NULL" not in query
        assert "AND deleted_at IS NULL" in query

    def test_delete_uses_canonical_document_deleted_event(self, monkeypatch):
        store = PostgresMetadataStore(SqlConfig(enabled=True))
        cursor = _FakeCursor()
        connection = _FakeConnection(cursor)

        @contextmanager
        def fake_connection():
            yield connection

        monkeypatch.setattr(store, "_connection", fake_connection)

        result = store.delete("doc-001")

        assert result.is_ok()
        assert connection.commit_calls == 1
        event_insert_params = cursor.execute_calls[1][1]
        assert event_insert_params[1] == OutboxEventType.DOCUMENT_DELETED.value

    def test_persist_curriculum_quiz_items_targets_lo_without_replacing_document(
        self,
        monkeypatch,
    ):
        store = PostgresMetadataStore(SqlConfig(enabled=True))
        cursor = _FakeCursor()
        connection = _FakeConnection(cursor)

        @contextmanager
        def fake_connection():
            yield connection

        monkeypatch.setattr(store, "_connection", fake_connection)

        result = store.persist_curriculum_quiz_items(
            lo_id="lo-001",
            bloom_level="analyze",
            quiz_items=[
                StoredQuizItem(
                    question_id="quiz-001",
                    document_id="doc-001",
                    primary_chunk_id="chunk-001",
                    source_chunk_ids=("chunk-001",),
                    heading_path=("Chapter 1",),
                    question="Which conclusion follows?",
                    choices=("A", "B", "C", "D"),
                    correct_index=2,
                    explanation="C follows from the source.",
                    difficulty="medium",
                    question_index=0,
                    model_id="gemini-test",
                )
            ],
        )

        assert result.is_ok()
        assert connection.commit_calls == 1
        assert len(cursor.execute_calls) == 0
        assert len(cursor.executemany_calls) == 1
        query, rows = cursor.executemany_calls[0]
        assert "INSERT INTO quiz_items" in query
        assert "UPDATE quiz_items SET deleted_at" not in query
        assert rows[0][8] == 4


def test_snapshot_soft_delete_and_outbox_are_one_transaction(monkeypatch):
    store = PostgresMetadataStore(SqlConfig(enabled=True))
    cursor = _FakeCursor()
    cursor.fetchone_result = (True,)
    connection = _FakeConnection(cursor)
    @contextmanager
    def connect():
        yield connection
    monkeypatch.setattr(store, '_connection', connect)
    result = store.upsert_chunks_with_outbox(
        [StoredChunkMetadata('new', 'doc', 0)], OutboxEventType.HEADING_GRAPH_PROJECT,
        'doc', {'replace_chunks': True})
    assert result.is_ok()
    cleanup = [(q, p) for q, p in cursor.execute_calls if 'UPDATE chunks SET deleted_at' in q]
    assert len(cleanup) == 1
    assert cleanup[0][1] == ('doc', ['new'])
    assert 'deleted_at = NULL' in cursor.executemany_calls[0][0]
    assert connection.commit_calls == 1

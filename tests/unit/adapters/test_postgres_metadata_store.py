from contextlib import contextmanager

from src.adapters.metadata.postgres_metadata_store import PostgresMetadataStore
from src.domain.ports.metadata_store import StoredChunkMetadata
from src.infrastructure.config import OutboxConfig, SqlConfig


class _FakeCursor:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list[tuple]]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, query: str, params: tuple) -> None:
        self.execute_calls.append((query, params))

    def executemany(self, query: str, rows: list[tuple]) -> None:
        self.executemany_calls.append((query, rows))


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.commit_calls = 0

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commit_calls += 1


class TestPostgresMetadataStore:
    def test_upsert_chunks_with_outbox_uses_one_commit(self, monkeypatch):
        store = PostgresMetadataStore(SqlConfig(enabled=True), OutboxConfig())
        cursor = _FakeCursor()
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
            event_type="heading_graph_project",
            aggregate_id="doc-001",
            payload={"document_id": "doc-001"},
        )

        assert result.is_ok()
        assert connection.commit_calls == 1
        assert len(cursor.executemany_calls) == 1
        assert "INSERT INTO chunks_metadata" in cursor.executemany_calls[0][0]
        assert len(cursor.execute_calls) == 1
        assert "INSERT INTO outbox_events" in cursor.execute_calls[0][0]

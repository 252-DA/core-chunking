import json
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
        # (lo, lo, quiz_id, lo, type, question, options, correct, explanation, bloom, …)
        assert rows[0][4] == "MCQ_SINGLE"
        assert rows[0][9] == 4

    def test_persist_curriculum_quiz_items_writes_type_and_typed_answer(self, monkeypatch):
        store = PostgresMetadataStore(SqlConfig(enabled=True))
        cursor = _FakeCursor()
        connection = _FakeConnection(cursor)

        @contextmanager
        def fake_connection():
            yield connection

        monkeypatch.setattr(store, "_connection", fake_connection)
        result = store.persist_curriculum_quiz_items(
            lo_id="00000000-0000-0000-0000-000000000001",
            bloom_level=3,
            quiz_items=[
                StoredQuizItem(
                    question_id="00000000-0000-0000-0000-000000000002",
                    document_id="doc",
                    primary_chunk_id="00000000-0000-0000-0000-000000000003",
                    question="Cây n đỉnh có ____ cạnh.",
                    question_type="FILL_BLANK",
                    correct_answer=["n-1", "n − 1"],
                )
            ],
        )

        assert result.is_ok()
        _, rows = cursor.executemany_calls[0]
        assert rows[0][4] == "FILL_BLANK"
        assert rows[0][6] is None
        assert json.loads(rows[0][7]) == ["n-1", "n − 1"]

    def test_persist_curriculum_lesson_cards_appends_in_card_order(self, monkeypatch):
        store = PostgresMetadataStore(SqlConfig(enabled=True))
        cursor = _FakeCursor()
        cursor.fetchone_result = (1,)  # LO hiện hành
        connection = _FakeConnection(cursor)

        @contextmanager
        def fake_connection():
            yield connection

        monkeypatch.setattr(store, "_connection", fake_connection)

        def card(card_id: str, index: int) -> StoredLessonCard:
            return StoredLessonCard(
                card_id=card_id,
                document_id="doc-001",
                primary_chunk_id="chunk-001",
                source_chunk_ids=("chunk-001",),
                title=f"Card {index}",
                bullets=("Ý một", "Ý hai"),
                key_insight="Ý chính.",
                example="Ví dụ.",
                check_question="Hỏi?",
                check_answer="Đáp.",
                card_index=index,
                model_id="gemini-test",
            )

        result = store.persist_curriculum_lesson_cards(
            lo_id="11111111-1111-4111-8111-111111111111",
            lesson_cards=[card("card-b", 1), card("card-a", 0)],
        )

        assert result.is_ok()
        assert connection.commit_calls == 1
        query, rows = cursor.executemany_calls[0]
        assert "INSERT INTO lesson_cards" in query
        assert "MAX(lc.position) + 1" in query
        assert "WHERE lesson_cards.status = 'GENERATED_DRAFT'" in query
        assert [row[4] for row in rows] == ["Card 0", "Card 1"]
        content = json.loads(rows[0][5])
        assert content["check_question"] == "Hỏi?"
        assert content["check_answer"] == "Đáp."
        assert content["example"] == "Ví dụ."
        assert content["misconception"] is None

    def test_persist_curriculum_lesson_cards_refuses_unknown_lo(self, monkeypatch):
        store = PostgresMetadataStore(SqlConfig(enabled=True))
        cursor = _FakeCursor()
        cursor.fetchone_result = None  # LO không tồn tại / không còn hiện hành
        connection = _FakeConnection(cursor)

        @contextmanager
        def fake_connection():
            yield connection

        monkeypatch.setattr(store, "_connection", fake_connection)

        result = store.persist_curriculum_lesson_cards(
            lo_id="11111111-1111-4111-8111-111111111111",
            lesson_cards=[
                StoredLessonCard(card_id="card-a", document_id="doc", primary_chunk_id="chunk")
            ],
        )

        assert result.is_err()
        assert "not current" in str(result.error)
        assert cursor.executemany_calls == []
        assert connection.commit_calls == 0


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


class TestAnswerGrading:
    """Chấm tự luận: chỉ chấm câu còn PENDING, mở AGS khi mọi câu của lượt đã GRADED."""

    def _store(self, monkeypatch, cursor):
        store = PostgresMetadataStore(SqlConfig(enabled=True))
        connection = _FakeConnection(cursor)

        @contextmanager
        def fake_connection():
            yield connection

        monkeypatch.setattr(store, "_connection", fake_connection)
        return store, connection

    def test_save_grade_only_touches_pending_rows(self, monkeypatch):
        cursor = _FakeCursor()
        cursor.rowcount = 0  # worker retry: câu đã được chấm ở lượt trước
        store, connection = self._store(monkeypatch, cursor)

        result = store.save_answer_grade(
            attempt_id="00000000-0000-0000-0000-000000000001",
            score=50,
            is_correct=False,
            feedback="Thiếu ý 2.",
        )

        assert result.unwrap() is False
        query, params = cursor.execute_calls[0]
        assert "grading_status = 'GRADED'" in query
        assert "grading_status = 'PENDING'" in query
        assert params[:3] == (50, False, "Thiếu ý 2.")
        assert connection.commit_calls == 1

    def test_needs_review_keeps_the_score_untouched(self, monkeypatch):
        cursor = _FakeCursor()
        cursor.rowcount = 1
        store, connection = self._store(monkeypatch, cursor)

        result = store.mark_answer_needs_review(
            attempt_id="00000000-0000-0000-0000-000000000001",
            feedback="Giảng viên sẽ chấm câu này.",
        )

        assert result.unwrap() is True
        query, params = cursor.execute_calls[0]
        assert "grading_status = 'NEEDS_REVIEW'" in query
        assert "score" not in query
        assert "grading_status = 'PENDING'" in query
        assert params[0] == "Giảng viên sẽ chấm câu này."
        assert connection.commit_calls == 1

    def test_release_waits_for_every_essay_of_the_submission(self, monkeypatch):
        cursor = _FakeCursor()
        cursor.rowcount = 3
        store, _ = self._store(monkeypatch, cursor)
        ids = ["00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"]

        assert store.release_graded_attempts(ids).unwrap() == 3
        lock, _ = cursor.execute_calls[0]
        assert "FOR UPDATE" in lock and "ORDER BY attempt_id" in lock
        query, params = cursor.execute_calls[1]
        assert "ags_status = 'AWAITING_GRADE'" in query
        # NEEDS_REVIEW cũng giữ cả lượt: lỗi chấm không được thành điểm trên LMS.
        assert "NOT EXISTS" in query and "grading_status <> 'GRADED'" in query
        assert params == (ids, ids)

    def test_list_answers_maps_rows(self, monkeypatch):
        cursor = _FakeCursor()
        cursor.fetchall_result = [
            ("a1", "q1", "c1", "SHORT_ANSWER", "Vì sao?", {"model_answer": "M", "rubric": ["R"]}, "Bài làm", "PENDING")
        ]
        store, _ = self._store(monkeypatch, cursor)

        [answer] = store.list_answers_for_grading(["00000000-0000-0000-0000-000000000001"]).unwrap()

        query, _ = cursor.execute_calls[0]
        assert "grading_input" in query and "quiz_items" not in query
        assert answer.question_type == "SHORT_ANSWER"
        assert answer.correct_answer == {"model_answer": "M", "rubric": ["R"]}
        assert answer.chosen_answer == "Bài làm"
        assert answer.grading_status == "PENDING"

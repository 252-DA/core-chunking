"""
Phần dữ liệu AI của enrichment trên Postgres thật: concepts + chunk_concepts +
sự kiện outbox trong một transaction, không đụng lesson_cards/quiz_items
(bảng của core-api, đi qua ENRICHMENT_CONTENT_RESULT).

Chạy khi có ``TEST_SQL_DSN`` trỏ tới database đã áp dụng migration; bỏ qua nếu không.
"""
import os

import pytest

from document_chunk.adapters.metadata.postgres_metadata_store import PostgresMetadataStore
from document_chunk.domain.exceptions import DocumentStaleError
from document_chunk.domain.outbox_events import OutboxEventType
from document_chunk.domain.ports.metadata_store import StoredChunkConcept, StoredConcept
from document_chunk.infrastructure.config import SqlConfig

_DSN = os.getenv("TEST_SQL_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="cần TEST_SQL_DSN")

COURSE = "01900000-0000-7000-8000-00000000e001"
LO = "01900000-0000-7000-8000-00000000e002"
DOC = "01900000-0000-7000-8000-00000000e010"
DELETED_DOC = "01900000-0000-7000-8000-00000000e020"
CHUNK = "01900000-0000-7000-8000-00000000e011"
LESSON = "01900000-0000-7000-8000-00000000e030"
CARD = "01900000-0000-7000-8000-00000000e031"
CONCEPT = "it-concept-dao-ham"


@pytest.fixture(scope="module")
def store():
    s = PostgresMetadataStore(SqlConfig(dsn=_DSN))
    with s._connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO courses (course_id, code, name) VALUES (%s, 'ENR1', 'Enrichment IT') "
            "ON CONFLICT DO NOTHING;", (COURSE,))
        cur.execute(
            "INSERT INTO learning_outcomes (lo_id, course_id, code, statement_vi) "
            "VALUES (%s, %s, 'L.O.1', 'Hiểu đạo hàm') ON CONFLICT DO NOTHING;", (LO, COURSE))
        for doc, deleted in ((DOC, False), (DELETED_DOC, True)):
            cur.execute(
                "INSERT INTO documents (document_id, course_id, title, file_path, status, deleted_at) "
                "VALUES (%s, %s, 't', %s, 'INDEXED', CASE WHEN %s THEN now() END) "
                "ON CONFLICT DO NOTHING;", (doc, COURSE, f"documents/{doc}.pdf", deleted))
        cur.execute(
            "INSERT INTO chunks (chunk_id, document_id, course_id, content, sort_order, heading_path) "
            "VALUES (%s, %s, %s, 'Đạo hàm', 0, '{Đạo hàm}') ON CONFLICT DO NOTHING;",
            (CHUNK, DOC, COURSE))
        cur.execute(
            "INSERT INTO lessons (lesson_id, course_id, lo_id, title) VALUES (%s, %s, %s, 'L') "
            "ON CONFLICT DO NOTHING;", (LESSON, COURSE, LO))
        cur.execute(
            "INSERT INTO lesson_cards (card_id, lesson_id, course_id, lo_id, title, content, source_chunk_ids) "
            "VALUES (%s, %s, %s, %s, 'nháp', '{}', ARRAY[%s]::uuid[]) ON CONFLICT (card_id) "
            "DO UPDATE SET deleted_at = NULL;", (CARD, LESSON, COURSE, LO, CHUNK))
        cur.execute("DELETE FROM outbox_events WHERE aggregate_id IN (%s, %s);", (DOC, DELETED_DOC))
        cur.execute("DELETE FROM chunk_concepts WHERE chunk_id = %s;", (CHUNK,))
        conn.commit()
    yield s
    s.close()


def _concepts():
    return (
        [StoredConcept(concept_id=CONCEPT, name="Đạo hàm", canonical_name="đạo hàm", slug=CONCEPT)],
        [StoredChunkConcept(chunk_id=CHUNK, concept_id=CONCEPT)],
    )


def _count(store, sql, *params):
    with store._connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def test_ai_rows_and_one_pending_event_are_written_together(store):
    concepts, mentions = _concepts()
    for _ in range(2):  # job chạy lại: thay chứ không nhân đôi
        event_id = store.persist_concept_enrichment(
            document_id=DOC,
            concepts=concepts,
            chunk_concepts=mentions,
            outbox_event_type=OutboxEventType.CONCEPT_GRAPH_PROJECT,
            outbox_payload={"document_id": DOC, "processing_generation": 1, "enrichment_attempt": 1},
        ).unwrap()

    assert event_id is not None
    assert _count(store, "SELECT count(*) FROM chunk_concepts WHERE chunk_id = %s", CHUNK) == 1
    assert _count(store, "SELECT count(*) FROM concepts WHERE id = %s", CONCEPT) == 1
    assert _count(
        store,
        "SELECT count(*) FROM outbox_events WHERE aggregate_id = %s AND status = 'PENDING'",
        DOC,
    ) == 1
    # Học liệu nháp của tài liệu không bị đụng: core-api thay nó khi nhận kết quả.
    assert _count(store, "SELECT count(*) FROM lesson_cards WHERE card_id = %s AND deleted_at IS NULL", CARD) == 1


def test_deleted_document_gets_no_ai_rows_or_event(store):
    concepts, mentions = _concepts()
    result = store.persist_concept_enrichment(
        document_id=DELETED_DOC,
        concepts=concepts,
        chunk_concepts=mentions,
        outbox_event_type=OutboxEventType.CONCEPT_GRAPH_PROJECT,
        outbox_payload={"document_id": DELETED_DOC},
    )
    assert isinstance(result.error, DocumentStaleError)
    assert _count(store, "SELECT count(*) FROM outbox_events WHERE aggregate_id = %s", DELETED_DOC) == 0

"""
Soft-delete chunks khi core-api xoá tài liệu (DOCUMENT_DELETED) trên Postgres thật.

core-api chỉ soft-delete ``documents``; ``chunks`` là bảng AI của worker nên worker
tự đặt ``chunks.deleted_at``. Hàm phải: chỉ đụng chunk còn sống của đúng tài liệu
đã xoá, idempotent, và không bao giờ ghi ``documents``.

Chạy khi có ``TEST_SQL_DSN`` trỏ tới database đã áp dụng migration; bỏ qua nếu không.
``TEST_SQL_DSN`` là tài khoản của worker (store chạy hàm đang test). Dữ liệu của
core-api (courses, documents) và các phép kiểm do ``TEST_SQL_ADMIN_DSN`` dựng/đọc,
mặc định trùng ``TEST_SQL_DSN`` — đặt riêng khi chạy bằng role chỉ ghi được bảng AI.
"""
import os
from contextlib import contextmanager

import psycopg
import pytest

from document_chunk.adapters.metadata.postgres_metadata_store import PostgresMetadataStore
from document_chunk.infrastructure.config import SqlConfig

_DSN = os.getenv("TEST_SQL_DSN")
_ADMIN_DSN = os.getenv("TEST_SQL_ADMIN_DSN") or _DSN
pytestmark = pytest.mark.skipif(not _DSN, reason="cần TEST_SQL_DSN")

COURSE = "01900000-0000-7000-8000-00000000f001"
DOC = "01900000-0000-7000-8000-00000000f010"
OTHER_DOC = "01900000-0000-7000-8000-00000000f020"
LIVE_CHUNK_A = "01900000-0000-7000-8000-00000000f011"
LIVE_CHUNK_B = "01900000-0000-7000-8000-00000000f012"
OLD_DELETED_CHUNK = "01900000-0000-7000-8000-00000000f013"
OTHER_CHUNK = "01900000-0000-7000-8000-00000000f021"
CONCEPT = "it-concept-soft-delete"


@contextmanager
def _admin():
    with psycopg.connect(_ADMIN_DSN) as conn, conn.cursor() as cur:
        yield cur  # commit khi thoát khối


@pytest.fixture
def store():
    s = PostgresMetadataStore(SqlConfig(dsn=_DSN))
    with _admin() as cur:
        # Dựng lại từ đầu mỗi test: các test đổi deleted_at nên không dùng chung được.
        cur.execute("DELETE FROM chunk_concepts WHERE chunk_id IN (%s, %s, %s, %s);",
                    (LIVE_CHUNK_A, LIVE_CHUNK_B, OLD_DELETED_CHUNK, OTHER_CHUNK))
        cur.execute("DELETE FROM chunks WHERE document_id IN (%s, %s);", (DOC, OTHER_DOC))
        cur.execute("DELETE FROM documents WHERE document_id IN (%s, %s);", (DOC, OTHER_DOC))
        cur.execute("DELETE FROM concepts WHERE id = %s;", (CONCEPT,))
        cur.execute(
            "INSERT INTO courses (course_id, code, name) VALUES (%s, 'SDEL1', 'Soft delete IT') "
            "ON CONFLICT DO NOTHING;", (COURSE,))
        for doc in (DOC, OTHER_DOC):
            cur.execute(
                "INSERT INTO documents (document_id, course_id, title, file_path, status) "
                "VALUES (%s, %s, 't', %s, 'INDEXED');", (doc, COURSE, f"documents/{doc}.pdf"))
        for chunk, doc, deleted in (
            (LIVE_CHUNK_A, DOC, False),
            (LIVE_CHUNK_B, DOC, False),
            (OLD_DELETED_CHUNK, DOC, True),  # đã ẩn từ trước (replace_chunks ở lần xử lý cũ)
            (OTHER_CHUNK, OTHER_DOC, False),
        ):
            cur.execute(
                "INSERT INTO chunks (chunk_id, document_id, course_id, content, sort_order, deleted_at) "
                "VALUES (%s, %s, %s, 'nội dung', 0, CASE WHEN %s THEN now() - interval '1 day' END);",
                (chunk, doc, COURSE, deleted))
        cur.execute(
            "INSERT INTO concepts (id, name, canonical_name, slug) VALUES (%s, 'c', 'c', %s);",
            (CONCEPT, CONCEPT))
        cur.execute("INSERT INTO chunk_concepts (chunk_id, concept_id) VALUES (%s, %s);",
                    (LIVE_CHUNK_A, CONCEPT))
    yield s
    s.close()


def _delete_document_like_core_api(store, document_id):
    """Phần việc của core-api (document.service.ts softDelete) mà worker chỉ quan sát."""
    with _admin() as cur:
        cur.execute("UPDATE documents SET deleted_at = now() WHERE document_id = %s;", (document_id,))


def _chunk_deleted_at(store, chunk_id):
    with _admin() as cur:
        cur.execute("SELECT deleted_at FROM chunks WHERE chunk_id = %s;", (chunk_id,))
        return cur.fetchone()[0]


def _document_row(store, document_id):
    with _admin() as cur:
        cur.execute(
            "SELECT status, deleted_at, chapter_code, processing_generation "
            "FROM documents WHERE document_id = %s;", (document_id,))
        return cur.fetchone()


def test_hides_only_the_live_chunks_of_the_deleted_document(store):
    _delete_document_like_core_api(store, DOC)
    old_stamp = _chunk_deleted_at(store, OLD_DELETED_CHUNK)

    hidden = store.soft_delete_document_chunks(DOC).unwrap()

    assert hidden == 2
    assert _chunk_deleted_at(store, LIVE_CHUNK_A) is not None
    assert _chunk_deleted_at(store, LIVE_CHUNK_B) is not None
    # chunk đã ẩn từ trước giữ nguyên dấu thời gian cũ, không bị ghi đè
    assert _chunk_deleted_at(store, OLD_DELETED_CHUNK) == old_stamp
    # tài liệu khác không bị đụng
    assert _chunk_deleted_at(store, OTHER_CHUNK) is None
    assert store.list_chunks(DOC).unwrap() == []
    assert [str(c.chunk_id) for c in store.list_chunks(OTHER_DOC).unwrap()] == [OTHER_CHUNK]


def test_second_call_is_a_no_op_and_keeps_the_first_timestamps(store):
    _delete_document_like_core_api(store, DOC)
    assert store.soft_delete_document_chunks(DOC).unwrap() == 2
    stamp = _chunk_deleted_at(store, LIVE_CHUNK_A)

    assert store.soft_delete_document_chunks(DOC).unwrap() == 0
    assert _chunk_deleted_at(store, LIVE_CHUNK_A) == stamp


def test_live_document_keeps_its_chunks(store):
    """Sự kiện đến nhầm cho tài liệu còn sống không được làm biến mất chunks của nó."""
    assert store.soft_delete_document_chunks(DOC).unwrap() == 0

    assert _chunk_deleted_at(store, LIVE_CHUNK_A) is None
    assert _chunk_deleted_at(store, LIVE_CHUNK_B) is None


def test_unknown_document_is_a_no_op(store):
    assert store.soft_delete_document_chunks("01900000-0000-7000-8000-00000000f0ff").unwrap() == 0


def test_never_writes_documents_or_other_ai_rows(store):
    _delete_document_like_core_api(store, DOC)
    before = _document_row(store, DOC)

    store.soft_delete_document_chunks(DOC).unwrap()

    assert _document_row(store, DOC) == before
    # chỉ chunks.deleted_at đổi; cạnh chunk → concept giữ nguyên (đường legacy cũ cũng vậy)
    with _admin() as cur:
        cur.execute("SELECT count(*) FROM chunk_concepts WHERE chunk_id = %s;", (LIVE_CHUNK_A,))
        assert cur.fetchone()[0] == 1

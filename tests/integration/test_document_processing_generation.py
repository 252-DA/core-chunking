"""
Đọc ``documents.processing_generation`` trên Postgres thật.

Worker dùng giá trị này để bỏ job của lượt xử lý cũ. Hàm chỉ SELECT: documents là
bảng của core-api. Chạy khi có ``TEST_SQL_DSN``; bỏ qua nếu không.
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

COURSE = "01900000-0000-7000-8000-00000000a001"
DOC = "01900000-0000-7000-8000-00000000a010"
DELETED_DOC = "01900000-0000-7000-8000-00000000a011"
MISSING_DOC = "01900000-0000-7000-8000-00000000a0ff"


@contextmanager
def _admin():
    with psycopg.connect(_ADMIN_DSN) as conn, conn.cursor() as cur:
        yield cur


@pytest.fixture
def store():
    s = PostgresMetadataStore(SqlConfig(dsn=_DSN))
    with _admin() as cur:
        cur.execute("DELETE FROM documents WHERE document_id IN (%s, %s);", (DOC, DELETED_DOC))
        cur.execute(
            "INSERT INTO courses (course_id, code, name) VALUES (%s, 'GEN1', 'Generation IT') "
            "ON CONFLICT DO NOTHING;", (COURSE,))
        cur.execute(
            "INSERT INTO documents (document_id, course_id, title, file_path, status, processing_generation) "
            "VALUES (%s, %s, 't', 'documents/a.pdf', 'QUEUED', 3);", (DOC, COURSE))
        cur.execute(
            "INSERT INTO documents (document_id, course_id, title, file_path, status, "
            "processing_generation, deleted_at) "
            "VALUES (%s, %s, 't', 'documents/b.pdf', 'INDEXED', 4, now());", (DELETED_DOC, COURSE))
    yield s
    s.close()


def test_reads_the_current_generation(store):
    assert store.get_document_processing_generation(DOC).unwrap() == 3


def test_follows_core_api_bumping_the_generation(store):
    with _admin() as cur:  # core-api: NEXT_PROCESSING_GENERATION khi đưa tài liệu vào hàng đợi lại
        cur.execute(
            "UPDATE documents SET processing_generation = processing_generation + 1 "
            "WHERE document_id = %s;", (DOC,))

    assert store.get_document_processing_generation(DOC).unwrap() == 4


def test_deleted_or_unknown_document_has_no_generation(store):
    assert store.get_document_processing_generation(DELETED_DOC).unwrap() is None
    assert store.get_document_processing_generation(MISSING_DOC).unwrap() is None


def test_does_not_write_documents(store):
    with _admin() as cur:
        cur.execute("SELECT status, processing_generation, processing_seq, deleted_at FROM documents WHERE document_id = %s;", (DOC,))
        before = cur.fetchone()

    store.get_document_processing_generation(DOC).unwrap()

    with _admin() as cur:
        cur.execute("SELECT status, processing_generation, processing_seq, deleted_at FROM documents WHERE document_id = %s;", (DOC,))
        assert cur.fetchone() == before

"""
Tài liệu học viên không được thấy (ẩn ở LMS hoặc đã xoá) trên Postgres thật.

``SearchChunksUseCase`` (chatbot qua mcp-service) loại các id này ra khỏi kết quả
Qdrant. Tài liệu xoá phải có trong tập đó: vector được dọn bất đồng bộ nên trước
khi dọn xong, tìm kiếm vẫn trúng chunk của tài liệu đã xoá.

Chạy khi có ``TEST_SQL_DSN`` trỏ tới database đã áp dụng migration; bỏ qua nếu không.
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

COURSE = "01900000-0000-7000-8000-00000000b001"
OTHER_COURSE = "01900000-0000-7000-8000-00000000b002"
LIVE = "01900000-0000-7000-8000-00000000b010"
UNPUBLISHED = "01900000-0000-7000-8000-00000000b011"
DELETED = "01900000-0000-7000-8000-00000000b012"
NOT_FROM_LMS = "01900000-0000-7000-8000-00000000b013"  # lms_published NULL: vẫn hiện
OTHER_COURSE_DELETED = "01900000-0000-7000-8000-00000000b020"
ALL_DOCS = (LIVE, UNPUBLISHED, DELETED, NOT_FROM_LMS, OTHER_COURSE_DELETED)


@contextmanager
def _admin():
    with psycopg.connect(_ADMIN_DSN) as conn, conn.cursor() as cur:
        yield cur


@pytest.fixture
def store():
    s = PostgresMetadataStore(SqlConfig(dsn=_DSN))
    with _admin() as cur:
        cur.execute("DELETE FROM documents WHERE document_id = ANY(%s::uuid[]);", (list(ALL_DOCS),))
        for course, code in ((COURSE, "HIDE1"), (OTHER_COURSE, "HIDE2")):
            cur.execute(
                "INSERT INTO courses (course_id, code, name) VALUES (%s, %s, 'Hidden IT') "
                "ON CONFLICT DO NOTHING;", (course, code))
        # (id, khoá học, lms_published, đã xoá)
        for doc, course, published, deleted in (
            (LIVE, COURSE, True, False),
            (UNPUBLISHED, COURSE, False, False),
            (DELETED, COURSE, True, True),
            (NOT_FROM_LMS, COURSE, None, False),
            (OTHER_COURSE_DELETED, OTHER_COURSE, True, True),
        ):
            cur.execute(
                "INSERT INTO documents (document_id, course_id, title, file_path, status, "
                "lms_published, deleted_at) "
                "VALUES (%s, %s, 't', %s, 'INDEXED', %s, CASE WHEN %s THEN now() END);",
                (doc, course, f"documents/{doc}.pdf", published, deleted))
    yield s
    s.close()


def test_hidden_ids_cover_unpublished_and_deleted_documents_of_the_course(store):
    hidden = store.list_hidden_document_ids("HIDE1").unwrap()

    assert sorted(hidden) == sorted([UNPUBLISHED, DELETED])


def test_live_and_not_from_lms_documents_stay_visible(store):
    hidden = set(store.list_hidden_document_ids("HIDE1").unwrap())

    assert LIVE not in hidden
    assert NOT_FROM_LMS not in hidden


def test_course_scope_excludes_other_courses(store):
    assert OTHER_COURSE_DELETED not in store.list_hidden_document_ids("HIDE1").unwrap()
    assert store.list_hidden_document_ids("HIDE2").unwrap() == [OTHER_COURSE_DELETED]


def test_course_given_as_uuid_resolves_the_same_way(store):
    assert sorted(store.list_hidden_document_ids(COURSE).unwrap()) == sorted([UNPUBLISHED, DELETED])


def test_without_course_covers_every_course(store):
    hidden = set(store.list_hidden_document_ids().unwrap())

    assert {UNPUBLISHED, DELETED, OTHER_COURSE_DELETED} <= hidden
    assert LIVE not in hidden and NOT_FROM_LMS not in hidden


def test_deleting_a_document_hides_it_even_though_qdrant_was_not_cleaned(store):
    """Tình huống lỗi cũ: tài liệu vừa xoá ở core-api, Qdrant chưa dọn."""
    assert LIVE not in store.list_hidden_document_ids("HIDE1").unwrap()

    with _admin() as cur:
        cur.execute("UPDATE documents SET deleted_at = now() WHERE document_id = %s;", (LIVE,))

    assert LIVE in store.list_hidden_document_ids("HIDE1").unwrap()

"""
Vị trí tài liệu trên Postgres thật: vai trò/chương của documents, dọn cạnh
chunk → LO suy luận, và thứ tự nguồn khi sinh quiz.

Chạy khi có ``TEST_SQL_DSN`` trỏ tới database đã áp dụng migration; bỏ qua nếu không.
"""
import os

import pytest

from document_chunk.adapters.metadata.postgres_metadata_store import PostgresMetadataStore
from document_chunk.domain.ports.metadata_store import StoredChunkLOMapping
from document_chunk.infrastructure.config import SqlConfig

_DSN = os.getenv("TEST_SQL_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="cần TEST_SQL_DSN")

COURSE = "01900000-0000-7000-8000-00000000d001"
CHAPTER = "01900000-0000-7000-8000-00000000d002"
LO = "01900000-0000-7000-8000-00000000d003"
LECTURE = "01900000-0000-7000-8000-00000000d010"
REFERENCE = "01900000-0000-7000-8000-00000000d020"
LECTURE_CHUNK = "01900000-0000-7000-8000-00000000d011"
REFERENCE_CHUNK = "01900000-0000-7000-8000-00000000d021"


@pytest.fixture(scope="module")
def store():
    s = PostgresMetadataStore(SqlConfig(dsn=_DSN))
    with s._connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO courses (course_id, code, name) VALUES (%s, 'PLC1', 'Placement') "
            "ON CONFLICT DO NOTHING;", (COURSE,))
        cur.execute(
            "INSERT INTO chapters (chapter_id, course_id, code, title, sort_order) "
            "VALUES (%s, %s, '1', 'Giới thiệu', 1) ON CONFLICT DO NOTHING;", (CHAPTER, COURSE))
        cur.execute(
            "INSERT INTO learning_outcomes (lo_id, course_id, code, statement_vi) "
            "VALUES (%s, %s, 'L.O.1', 'Hiểu dữ liệu lớn') ON CONFLICT DO NOTHING;", (LO, COURSE))
        for doc, role, chunk in ((LECTURE, "lecture", LECTURE_CHUNK), (REFERENCE, "reference", REFERENCE_CHUNK)):
            cur.execute(
                "INSERT INTO documents (document_id, course_id, title, file_path, status, role) "
                "VALUES (%s, %s, %s, %s, 'INDEXED', %s) ON CONFLICT DO NOTHING;",
                (doc, COURSE, role, f"documents/{doc}.pdf", role))
            cur.execute(
                "INSERT INTO chunks (chunk_id, document_id, course_id, content, sort_order, heading_path) "
                "VALUES (%s, %s, %s, %s, 0, '{}') ON CONFLICT DO NOTHING;",
                (chunk, doc, COURSE, f"{role} text"))
        # Chạy lại trên cùng database: đưa về trạng thái ban đầu.
        cur.execute(
            "UPDATE documents SET chapter_code = NULL, chapter_provenance = NULL, "
            "chapter_confidence = NULL, chapter_reason = NULL WHERE document_id IN (%s, %s);",
            (LECTURE, REFERENCE))
        cur.execute("DELETE FROM chunk_lo_mappings WHERE lo_id = %s;", (LO,))
        conn.commit()
    yield s
    s.close()


def test_placement_defaults_to_lecture_without_chapter(store):
    placement = store.get_document_placement(LECTURE).unwrap()
    assert (placement.role, placement.chapter_code, placement.chapter_provenance) == ("lecture", None, None)


def test_system_chapter_never_overrides_teacher_choice(store):
    assert store.set_document_chapter(LECTURE, "1", "content", 0.617, "khớp nội dung").unwrap() is True
    placement = store.get_document_placement(LECTURE).unwrap()
    assert (placement.chapter_code, placement.chapter_provenance, placement.chapter_confidence) == ("1", "content", 0.62)

    with store._connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE documents SET chapter_code = '2', chapter_provenance = 'confirmed' "
                    "WHERE document_id = %s;", (LECTURE,))
        conn.commit()
    assert store.set_document_chapter(LECTURE, "1", "content", 0.9, "khớp nội dung").unwrap() is False
    assert store.get_document_placement(LECTURE).unwrap().chapter_code == "2"


def test_lecture_chunks_come_before_reference_chunks(store):
    store.upsert_chunk_lo_mappings([
        StoredChunkLOMapping(chunk_id=REFERENCE_CHUNK, lo_id=LO, confidence=0.7, source="section"),
        StoredChunkLOMapping(chunk_id=LECTURE_CHUNK, lo_id=LO, confidence=0.5, source="document"),
    ]).unwrap()
    ordered = [str(c.chunk_id) for c in store.list_chunks_for_lo(LO).unwrap()]
    assert ordered == [LECTURE_CHUNK, REFERENCE_CHUNK]


def test_clearing_inferred_mappings_keeps_confirmed_ones(store):
    store.upsert_chunk_lo_mappings([
        StoredChunkLOMapping(chunk_id=LECTURE_CHUNK, lo_id=LO, confidence=0.5, source="document"),
    ]).unwrap()
    with store._connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE chunk_lo_mappings SET provenance = 'confirmed' WHERE chunk_id = %s;",
                    (REFERENCE_CHUNK,))
        conn.commit()

    assert store.delete_inferred_chunk_lo_mappings(LECTURE).unwrap() == 1
    assert store.delete_inferred_chunk_lo_mappings(REFERENCE).unwrap() == 0
    assert [str(c.chunk_id) for c in store.list_chunks_for_lo(LO).unwrap()] == [REFERENCE_CHUNK]

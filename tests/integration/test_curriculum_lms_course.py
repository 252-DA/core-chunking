"""
Đề cương gắn vào học phần do LMS tạo trước (LTI launch), không đẻ học phần mới.

Học phần LTI có course_id là uuidv7 và mã "canvas-…", còn đề cương mang mã
DCMH. Worker truyền UUID của học phần LTI làm course_id_hint; ghi và đọc đều
phải bám vào đúng hàng đó.

Chạy khi có ``TEST_SQL_DSN``; bỏ qua nếu không.
"""
import os
from dataclasses import replace
from pathlib import Path

import pytest

from document_chunk.adapters.curriculum.dcmh_extractor import DcmhExtractor
from document_chunk.adapters.graph.noop_graph_store import NoopGraphStore
from document_chunk.adapters.metadata.postgres_metadata_store import PostgresMetadataStore
from document_chunk.adapters.metadata.postgres_repository_utils import stable_uuid
from document_chunk.application.use_cases.ingest_curriculum import IngestCurriculumUseCase
from document_chunk.domain.entities.document import Document, DocumentType, ParsedDocument
from document_chunk.infrastructure.config import SqlConfig

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "syllabus" / "DCMH.CO3011.pdf"
_DSN = os.getenv("TEST_SQL_DSN")

# Cố định để chạy lại vẫn upsert vào cùng các hàng.
_LMS_COURSE_ID = "01900000-0000-7000-8000-00000000c0de"
_LMS_CODE = "canvas-test-lms-course"
# Mã DCMH riêng cho test này, để không đụng khoá unique courses.code với CO3011
# mà test_curriculum_persistence ghi vào cùng database.
_DCMH_CODE = "CO3011-LMS"

pytestmark = pytest.mark.skipif(
    not _DSN or not _FIXTURE.exists(),
    reason="cần TEST_SQL_DSN và fixture đề cương",
)


@pytest.fixture(scope="module")
def store():
    s = PostgresMetadataStore(SqlConfig(dsn=_DSN))
    yield s
    s.close()


@pytest.fixture(scope="module")
def ingested(store):
    with store._connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO courses (course_id, lms_id, code, name) "
                "VALUES (%s::uuid, %s, %s, %s) ON CONFLICT (course_id) DO NOTHING;",
                (_LMS_COURSE_ID, f"canvas:{_LMS_CODE}", _LMS_CODE, "LMS course"),
            )
        conn.commit()

    parsed = ParsedDocument(
        document=Document(
            id="22222222-2222-7222-8222-222222222222",
            name=_FIXTURE.name,
            path=_FIXTURE,
            doc_type=DocumentType.PDF,
            size_bytes=_FIXTURE.stat().st_size,
            mime_type="application/pdf",
        ),
        sections=[],
        page_count=14,
    )
    curriculum = DcmhExtractor().extract(parsed, course_id_hint=_LMS_COURSE_ID).unwrap()
    curriculum = replace(curriculum, course=replace(curriculum.course, code=_DCMH_CODE))

    use_case = IngestCurriculumUseCase(
        parsers=[],
        curriculum_extractor=None,
        metadata_store=store,
        graph_store=NoopGraphStore(),
    )
    result = use_case._persist_to_postgres(curriculum)
    assert result.is_ok(), str(result.error)
    return curriculum


def test_curriculum_lands_on_the_lms_course(store, ingested):
    with store._connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM courses WHERE course_id = %s::uuid;", (_LMS_COURSE_ID,))
            assert cur.fetchone()[0] == _DCMH_CODE
            # Trước đây học phần được tra theo mã DCMH, không thấy thì sinh hàng
            # mới stable_uuid(mã) và treo cả đề cương lên đó.
            cur.execute(
                "SELECT count(*) FROM courses WHERE course_id = %s::uuid;",
                (stable_uuid(_DCMH_CODE),),
            )
            assert cur.fetchone()[0] == 0, "ghi ra học phần mới thay vì học phần LMS"
            cur.execute(
                "SELECT count(*) FROM learning_outcomes WHERE course_id = %s::uuid;",
                (_LMS_COURSE_ID,),
            )
            assert cur.fetchone()[0] == 9


def test_reads_by_lms_course_uuid(store, ingested):
    """MapChunksToLos tra LO bằng course_id của tài liệu, tức UUID học phần LMS."""
    result = store.get_curriculum(_LMS_COURSE_ID)
    assert result.is_ok(), str(result.error)
    _, chapters, los, _ = result.unwrap()
    assert len(chapters) == 12
    assert len(los) == 9

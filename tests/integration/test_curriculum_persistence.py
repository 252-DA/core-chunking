"""
Round-trip đề cương: PDF thật → DcmhExtractor → Postgres → đọc lại.

Chạy khi có ``TEST_SQL_DSN`` trỏ tới một database đã áp dụng migration; bỏ qua
nếu không. Mục đích là chứng minh hình dạng nhiều–nhiều sống được trong DB thật:
một LO gắn nhiều chương mà vẫn là MỘT hàng ``learning_outcomes``.
"""
import os
from pathlib import Path

import pytest

from document_chunk.adapters.curriculum.dcmh_extractor import DcmhExtractor
from document_chunk.adapters.metadata.postgres_metadata_store import PostgresMetadataStore
from document_chunk.application.use_cases.ingest_curriculum import IngestCurriculumUseCase
from document_chunk.adapters.graph.noop_graph_store import NoopGraphStore
from document_chunk.domain.entities.document import Document, DocumentType, ParsedDocument
from document_chunk.infrastructure.config import SqlConfig

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "syllabus" / "DCMH.CO3011.pdf"
_DSN = os.getenv("TEST_SQL_DSN")

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
    parsed = ParsedDocument(
        document=Document(
            id="11111111-1111-7111-8111-111111111111",
            name=_FIXTURE.name,
            path=_FIXTURE,
            doc_type=DocumentType.PDF,
            size_bytes=_FIXTURE.stat().st_size,
            mime_type="application/pdf",
        ),
        sections=[],
        page_count=14,
    )
    curriculum = DcmhExtractor().extract(parsed, course_id_hint="CO3011").unwrap()

    use_case = IngestCurriculumUseCase(
        parsers=[],
        curriculum_extractor=None,
        metadata_store=store,
        graph_store=NoopGraphStore(),
    )
    result = use_case._persist_to_postgres(curriculum)
    assert result.is_ok(), str(result.error)
    return curriculum


def test_one_lo_row_per_code(store, ingested):
    """L.O.2.2 dạy ở 4 chương nhưng vẫn là một hàng — khoá unique cũ tạo ra 4."""
    result = store.get_curriculum("CO3011")
    assert result.is_ok(), str(result.error)
    _, _, los, _ = result.unwrap()

    codes = [lo.code for lo in los]
    assert len(codes) == len(set(codes)) == 9
    assert codes.count("L.O.2.2") == 1


def test_chapter_lo_links_round_trip(store, ingested):
    result = store.list_chapter_lo_links("CO3011")
    assert result.is_ok(), str(result.error)
    links = result.unwrap()
    assert len(links) == 12

    chapters = store.get_curriculum("CO3011").unwrap()[1]
    code_by_id = {c.chapter_id: c.code for c in chapters}
    los = store.get_curriculum("CO3011").unwrap()[2]
    lo_code_by_id = {lo.lo_id: lo.code for lo in los}

    by_lo: dict[str, list[str]] = {}
    for link in links:
        by_lo.setdefault(lo_code_by_id[link.lo_id], []).append(code_by_id[link.chapter_id])
    assert sorted(by_lo["L.O.2.2"], key=int) == ["4", "5", "7", "12"]
    assert sorted(by_lo["L.O.2.1"], key=int) == ["6", "8", "9"]


def test_list_los_by_chapter_uses_the_join_table(store, ingested):
    """Chương 7 phải trả L.O.2.2. Luật cũ (code LIKE 'L.O.7.%') trả rỗng."""
    result = store.list_los_by_chapter("CO3011", "7")
    assert result.is_ok(), str(result.error)
    assert [lo.code for lo in result.unwrap()] == ["L.O.2.2"]


def test_list_chapters_for_lo(store, ingested):
    result = store.list_chapters_for_lo("CO3011", "L.O.2.2")
    assert result.is_ok(), str(result.error)
    assert [c.code for c in result.unwrap()] == ["4", "5", "7", "12"]


def test_lo_without_chapter_is_still_stored(store, ingested):
    """L.O.3.1 / L.O.3.2 không chương nào dạy — vẫn phải có mặt, không biến mất."""
    los = store.get_curriculum("CO3011").unwrap()[2]
    assert {"L.O.3.1", "L.O.3.2"} <= {lo.code for lo in los}

    links = store.list_chapter_lo_links("CO3011").unwrap()
    lo_code_by_id = {lo.lo_id: lo.code for lo in los}
    linked = {lo_code_by_id[l.lo_id] for l in links}
    assert "L.O.3.1" not in linked


def test_cdio_stays_null_and_bloom_marked_inferred(store, ingested):
    los = store.get_curriculum("CO3011").unwrap()[2]
    for lo in los:
        assert lo.cdio_level is None, f"{lo.code} có CDIO bịa"
        assert lo.bloom_provenance == "inferred"


def test_assessment_tree_survives_round_trip(store, ingested):
    assessments = store.get_curriculum("CO3011").unwrap()[3]
    by_code = {a.code: a for a in assessments}
    assert set(by_code) == {"A.O.1", "A.O.1.1", "A.O.2"}
    assert by_code["A.O.1.1"].parent_code == "A.O.1"
    assert by_code["A.O.1"].activity_type == "GPJ"


def test_list_los_by_assessment_matches_on_code(store, ingested):
    result = store.list_los_by_assessment("CO3011", "A.O.1.1")
    assert result.is_ok(), str(result.error)
    assert {lo.code for lo in result.unwrap()} == {"L.O.2.2"}


def test_extraction_issues_are_persisted_for_review(store, ingested):
    with store._connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code, severity FROM curriculum_extraction_issues "
                "WHERE course_id = (SELECT course_id FROM courses WHERE code='CO3011')"
            )
            rows = cur.fetchall()
    codes = {r[0] for r in rows}
    assert "lo.not_taught" in codes
    assert "goal.code_collision" in codes
    assert all(r[1] != "error" for r in rows)

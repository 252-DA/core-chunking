"""
D1 end-to-end: `chunk_lo_mappings` thực sự được ghi, và sinh quiz theo LO tìm
thấy nguồn.

Đây là mắt xích mà quiz-generation-redesign.md gọi là "điều kiện để mọi thứ khác
có ý nghĩa": `MapChunksToLosUseCase` trước đây chỉ được khai báo trong container
mà không nơi nào gọi, nên bảng luôn rỗng và sinh quiz theo LO luôn dừng ở
"No grounded source chunks". Unit test mock `list_chunks_for_lo` nên không thấy.

Chạy khi có ``TEST_SQL_DSN`` trỏ tới database đã áp dụng migration.
"""
import os
import uuid

import pytest

from document_chunk.adapters.curriculum.heuristic_lo_mapper import HeuristicLoMapper
from document_chunk.adapters.graph.noop_graph_store import NoopGraphStore
from document_chunk.adapters.metadata.postgres_metadata_store import PostgresMetadataStore
from document_chunk.application.use_cases.map_chunks_to_los import (
    MapChunksToLosRequest,
    MapChunksToLosUseCase,
)
from document_chunk.domain.entities.curriculum import (
    Chapter,
    ChapterLOLink,
    Course,
    Curriculum,
    LearningOutcome,
)
from document_chunk.infrastructure.config import SqlConfig

_DSN = os.getenv("TEST_SQL_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="cần TEST_SQL_DSN")

_COURSE = "CO3011"
_DOC = "33333333-3333-7333-8333-333333333333"

# Chương 7 dạy L.O.2.2 — số chương và số trong mã LO cố tình lệch nhau.
_CHAPTERS = {"6": "Lập kế hoạch hoạt động", "7": "Quản lý rủi ro", "10": "Quản lý nhân sự"}
_LINKS = [("6", "L.O.2.1"), ("7", "L.O.2.2"), ("10", "L.O.3")]
_LOS = {
    "L.O.2.1": "Sử dụng được các công cụ tiên tiến quản lý dự án phần mềm",
    "L.O.2.2": "Tổng hợp, phân tích, đánh giá, đề xuất giải pháp cải tiến",
    "L.O.3": "Giao tiếp hiệu quả trong nhóm dự án",
}
# Heading của tài liệu giảng dạy: mỗi kiểu đánh số một khác.
_CHUNKS = [
    ("Chương 7", "7.10 Áp dụng kỹ thuật PERT"),
    ("Bài 7", "Mô phỏng Monte Carlo"),
    ("Chương 6", "6.12 Đường găng"),
    ("Phụ lục", "Bảng ký hiệu"),          # không thuộc chương nào
]


@pytest.fixture(scope="module")
def store():
    s = PostgresMetadataStore(SqlConfig(dsn=_DSN))
    yield s
    s.close()


@pytest.fixture(scope="module")
def curriculum():
    return Curriculum(
        course=Course(course_id=_COURSE, code=_COURSE, title_vi="Quản lý Dự án Phần mềm"),
        chapters=tuple(
            Chapter(chapter_id=f"{_COURSE}:CH{c}", code=c, title=t, order_index=int(c))
            for c, t in _CHAPTERS.items()
        ),
        learning_outcomes=tuple(
            LearningOutcome(lo_id=f"{_COURSE}:{c}", code=c, parent_code=None, statement_vi=s)
            for c, s in _LOS.items()
        ),
        assessments=(),
        chapter_lo_links=tuple(ChapterLOLink(chapter_code=ch, lo_code=lo) for ch, lo in _LINKS),
    )


@pytest.fixture(scope="module")
def seeded(store, curriculum):
    """Ghi đề cương và một tài liệu giảng dạy đã index."""
    from document_chunk.adapters.metadata.postgres_repository_utils import stable_uuid
    from document_chunk.application.use_cases.ingest_curriculum import IngestCurriculumUseCase

    use_case = IngestCurriculumUseCase(
        parsers=[], curriculum_extractor=None,
        metadata_store=store, graph_store=NoopGraphStore(),
    )
    assert use_case._persist_to_postgres(curriculum).is_ok()

    course_uuid = stable_uuid(_COURSE)
    with store._connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO documents (document_id, course_id, title, file_path, status) "
                "VALUES (%s::uuid, %s::uuid, 'Slide QLDA', 'minio://slides.pdf', 'INDEXED') "
                "ON CONFLICT (document_id) DO NOTHING;",
                (_DOC, course_uuid),
            )
            for i, heading in enumerate(_CHUNKS):
                cur.execute(
                    "INSERT INTO chunks (chunk_id, document_id, course_id, content, "
                    "heading_path, page_number, sort_order, language) "
                    "VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, 'vi') "
                    "ON CONFLICT (chunk_id) DO NOTHING;",
                    (str(uuid.uuid5(uuid.NAMESPACE_URL, f"chunk-{i}")), _DOC, course_uuid,
                     f"Nội dung {i}", list(heading), 1, i),
                )
        conn.commit()
    return course_uuid


def test_mapping_writes_rows(store, seeded):
    """Trước khi nối mapper vào pipeline, bảng này luôn rỗng."""
    with store._connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chunk_lo_mappings;")
        conn.commit()

    result = MapChunksToLosUseCase(
        metadata_store=store, graph_store=NoopGraphStore(), lo_mapper=HeuristicLoMapper(),
    ).execute(MapChunksToLosRequest(document_id=_DOC, course_id=_COURSE))

    assert result.is_ok(), str(result.error)
    assert result.unwrap().mapping_count > 0

    with store._connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM chunk_lo_mappings;")
            assert cur.fetchone()[0] > 0


def test_chapter_7_chunks_map_to_lo_2_2(store, seeded):
    """Chương 7 dạy L.O.2.2, không phải "L.O.7.x"."""
    with store._connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT lo.code, c.heading_path[1] "
                "FROM chunk_lo_mappings m "
                "JOIN learning_outcomes lo ON lo.lo_id = m.lo_id "
                "JOIN chunks c ON c.chunk_id = m.chunk_id "
                "ORDER BY 2;"
            )
            rows = cur.fetchall()
    by_heading = {h: code for code, h in rows}
    assert by_heading["Chương 7"] == "L.O.2.2"
    assert by_heading["Bài 7"] == "L.O.2.2"      # D2: "Bài N" cũng phải nhận ra
    assert by_heading["Chương 6"] == "L.O.2.1"
    assert "Phụ lục" not in by_heading           # không đoán bừa


def test_quiz_generation_now_finds_grounded_context(store, seeded):
    """
    Đây là câu trả lời cho "No grounded source chunks": `list_chunks_for_lo` là
    hàm mà GenerateCurriculumQuizUseCase dùng để lấy ngữ cảnh.
    """
    from document_chunk.adapters.metadata.postgres_repository_utils import stable_uuid

    lo_uuid = stable_uuid(f"{_COURSE}:L.O.2.2")
    result = store.list_chunks_for_lo(lo_uuid)
    assert result.is_ok(), str(result.error)
    chunks = result.unwrap()
    assert len(chunks) >= 2
    assert {c.heading_path[0] for c in chunks} == {"Chương 7", "Bài 7"}


def test_mapping_records_signal_and_stays_inferred(store, seeded):
    """Phỏng đoán của hệ thống phải phân biệt được với liên kết đã xác nhận."""
    with store._connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT source, provenance FROM chunk_lo_mappings;")
            rows = cur.fetchall()
    assert rows
    assert all(p == "inferred" for _, p in rows)
    assert all(s.startswith("chapter") for s, _ in rows)


def test_confirmed_edges_are_not_downgraded_by_a_rerun(store, seeded):
    """Giảng viên xác nhận xong, chạy lại pipeline không được hạ về phỏng đoán."""
    with store._connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chunk_lo_mappings SET provenance = 'confirmed' "
                "WHERE ctid IN (SELECT ctid FROM chunk_lo_mappings LIMIT 1) "
                "RETURNING chunk_id::text, lo_id::text;"
            )
            pinned = cur.fetchone()
        conn.commit()

    MapChunksToLosUseCase(
        metadata_store=store, graph_store=NoopGraphStore(), lo_mapper=HeuristicLoMapper(),
    ).execute(MapChunksToLosRequest(document_id=_DOC, course_id=_COURSE))

    with store._connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provenance FROM chunk_lo_mappings "
                "WHERE chunk_id = %s::uuid AND lo_id = %s::uuid;",
                pinned,
            )
            assert cur.fetchone()[0] == "confirmed"

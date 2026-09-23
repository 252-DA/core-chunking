"""
Tests cho DcmhExtractor — chạy trên đề cương THẬT (CO3011, HK261).

Dùng file thật thay vì text tổng hợp: text tổng hợp được viết cho vừa regex nên
luôn xanh, còn cái làm hỏng bản trích là bố cục thật — bảng nhiều cột, hàng kéo
dài qua trang, và ba con số khác nghĩa cùng nằm trong một hàng.

Sự thật nền (đọc tay từ file):
  - mục 4.1: 2 mục tiêu, dùng lại ký hiệu L.O.1 / L.O.2 với nội dung khác 4.2
  - mục 4.2: 9 LO — 3 cha và 6 con
  - mục 5.2: 3 AO — A.O.1, A.O.1.1 (con của A.O.1), A.O.2
  - mục 6  : 15 buổi, 12 chương; buổi 7 / 14 / 15 không gắn chương
"""
from pathlib import Path

import pytest

from document_chunk.adapters.curriculum.dcmh_extractor import DcmhExtractor
from document_chunk.domain.entities.document import (
    Document,
    DocumentType,
    ParsedDocument,
)

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "syllabus" / "DCMH.CO3011.pdf"

# Quan hệ chương ↔ LO đúng như bảng mục 6 ghi. Chú ý: KHÔNG suy được từ mã LO —
# L.O.2.2 nằm ở chương 4, 5, 7, 12 chứ không phải chương 2.
_CHAPTER_TO_LO = {
    "1": ["L.O.1.1"],
    "2": ["L.O.1.2"],
    "3": ["L.O.1.2"],
    "4": ["L.O.2.2"],
    "5": ["L.O.2.2"],
    "6": ["L.O.2.1"],
    "7": ["L.O.2.2"],
    "8": ["L.O.2.1"],
    "9": ["L.O.2.1"],
    "10": ["L.O.3"],
    "11": ["L.O.3"],
    "12": ["L.O.2.2"],
}


def _parsed_doc(path: Path) -> ParsedDocument:
    return ParsedDocument(
        document=Document(
            id="dcmh-co3011",
            name=path.name,
            path=path,
            doc_type=DocumentType.PDF,
            size_bytes=path.stat().st_size,
            mime_type="application/pdf",
        ),
        sections=[],
        page_count=14,
    )


@pytest.fixture(scope="module")
def curriculum():
    if not _FIXTURE.exists():  # pragma: no cover
        pytest.skip(f"thiếu fixture đề cương: {_FIXTURE}")
    result = DcmhExtractor().extract(_parsed_doc(_FIXTURE), course_id_hint="CO3011")
    assert result.is_ok(), str(result.error)
    return result.unwrap()


# ---------------------------------------------------------------------------
# Thông tin học phần
# ---------------------------------------------------------------------------

def test_course_header(curriculum):
    course = curriculum.course
    assert course.code == "CO3011"
    assert course.title_vi == "Quản lý Dự án Phần mềm"
    assert course.title_en == "Software Project Management"
    assert course.credits == 3
    assert course.semester == "HK261"
    assert course.syllabus_version == "DCMH.CO3011.9.1"
    assert course.lo_year == "2026"


# ---------------------------------------------------------------------------
# Mục 4.1 vs 4.2 — cùng ký hiệu, khác nội dung
# ---------------------------------------------------------------------------

def test_goals_kept_separate_from_learning_outcomes(curriculum):
    goals = {g.code: g.statement_vi for g in curriculum.goals}
    assert set(goals) == {"L.O.1", "L.O.2"}
    assert goals["L.O.1"].startswith("Hiểu được các khái niệm cơ bản")

    lo = curriculum.lo_by_code["L.O.1"]
    assert lo.statement_vi.startswith("Hiểu rõ để áp dụng được")
    assert lo.statement_vi != goals["L.O.1"]


def test_code_collision_between_4_1_and_4_2_is_reported(curriculum):
    collisions = {i.code for i in curriculum.issues if i.code == "goal.code_collision"}
    assert collisions == {"goal.code_collision"}


# ---------------------------------------------------------------------------
# Mục 4.2 — chuẩn đầu ra
# ---------------------------------------------------------------------------

def test_all_nine_learning_outcomes_extracted(curriculum):
    assert [lo.code for lo in curriculum.learning_outcomes] == [
        "L.O.1", "L.O.1.1", "L.O.1.2",
        "L.O.2", "L.O.2.1", "L.O.2.2",
        "L.O.3", "L.O.3.1", "L.O.3.2",
    ]


def test_lo_tree_uses_parent_code(curriculum):
    by_code = curriculum.lo_by_code
    assert by_code["L.O.1"].parent_code is None
    assert by_code["L.O.2.2"].parent_code == "L.O.2"
    assert by_code["L.O.3.2"].parent_code == "L.O.3"


def test_bilingual_lo_is_one_entity_with_two_statements(curriculum):
    lo = curriculum.lo_by_code["L.O.2.2"]
    assert lo.statement_vi.startswith("Tổng hợp, phân tích, đánh giá")
    assert lo.statement_en is not None
    assert lo.statement_en.startswith("Synthesize, analyze, and evaluate")
    # Bản dịch của L.O.2.2 nằm vắt qua ranh giới trang 3–4 trong file gốc.
    assert "software project management" in lo.statement_en.lower()


def test_every_lo_carries_a_source_reference(curriculum):
    for lo in curriculum.learning_outcomes:
        assert lo.source is not None
        assert lo.source.section == "4.2"
        assert lo.source.page in (3, 4)


def test_bloom_and_cdio_are_marked_as_system_suggestions(curriculum):
    for lo in curriculum.learning_outcomes:
        # Đề cương không ghi Bloom/CDIO ⇒ không được lưu như dữ kiện từ tài liệu.
        assert lo.bloom_provenance == "inferred"
        assert lo.cdio_provenance == "inferred"
        assert lo.cdio_level is None


# ---------------------------------------------------------------------------
# Mục 5.2 / 5.3 — hoạt động đánh giá
# ---------------------------------------------------------------------------

def test_multi_level_assessment_code_is_kept(curriculum):
    codes = [a.code for a in curriculum.assessments]
    assert codes == ["A.O.1", "A.O.1.1", "A.O.2"]


def test_assessment_tree_and_activity_type(curriculum):
    by_code = curriculum.assessment_by_code
    assert by_code["A.O.1"].parent_code is None
    assert by_code["A.O.1.1"].parent_code == "A.O.1"
    assert by_code["A.O.1"].activity_type == "GPJ"
    assert by_code["A.O.2"].activity_type == "EXM"
    assert by_code["A.O.2"].category == "final"


def test_course_ratios_are_not_turned_into_edge_weights(curriculum):
    # 40% bài tập lớn / 60% thi cuối kỳ là tỷ trọng hình thức học tập ở mục 1.1,
    # không phải trọng số của từng cạnh LO–AO.
    for a in curriculum.assessments:
        assert a.weight is None
        assert a.weight_provenance == "inferred"
    for link in curriculum.lo_assessment_links:
        assert not hasattr(link, "weight") or getattr(link, "weight", None) is None


def test_section_5_3_pairs_extracted(curriculum):
    course_wide = {
        (l.lo_code, l.assessment_code)
        for l in curriculum.lo_assessment_links
        if l.row_order_index is None
    }
    assert ("L.O.2.2", "A.O.1.1") in course_wide
    assert ("L.O.3", "A.O.2") in course_wide
    assert len(course_wide) == 11


# ---------------------------------------------------------------------------
# Mục 6 — bảng nội dung chi tiết
# ---------------------------------------------------------------------------

def test_twelve_chapters_with_titles(curriculum):
    assert [c.code for c in curriculum.chapters] == [str(n) for n in range(1, 13)]
    by_code = curriculum.chapter_by_code
    assert by_code["7"].title == "Quản lý rủi ro"
    assert by_code["12"].title == "Chất lượng phần mềm"


def test_sessions_and_chapters_are_separate_axes(curriculum):
    assert len(curriculum.sessions) == 15
    assert len(curriculum.chapters) == 12

    by_order = {s.order_index: s for s in curriculum.sessions}
    # Buổi 7 là ôn bài/nghỉ giữa kỳ — không thuộc chương nào.
    assert by_order[7].chapter_code is None
    assert by_order[7].title_vi.startswith("Ôn bài")
    assert by_order[7].lo_codes == ()
    # Từ đó số buổi lệch số chương: buổi 8 dạy chương 7.
    assert by_order[8].chapter_code == "7"
    assert by_order[13].chapter_code == "12"
    assert by_order[14].chapter_code is None
    assert by_order[15].chapter_code is None


def test_explicit_session_numbers_match_row_position(curriculum):
    by_order = {s.order_index: s for s in curriculum.sessions}
    for order in (7, 14, 15):
        assert by_order[order].session_no == order
    assert not [i for i in curriculum.issues if i.code == "session.number_mismatch"]


def test_page_spanning_row_does_not_create_a_duplicate(curriculum):
    # Nội dung chương 3, 6, 8, 10, 12 bị cắt sang trang sau; phần nối tiếp có ô
    # "Buổi" rỗng và không được sinh thành hàng mới.
    orders = [s.order_index for s in curriculum.sessions]
    assert orders == list(range(1, 16))


# ---------------------------------------------------------------------------
# Quan hệ chương ↔ LO — điểm chính
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("chapter_code,expected", sorted(_CHAPTER_TO_LO.items()))
def test_chapter_to_lo_matches_document(curriculum, chapter_code, expected):
    assert list(curriculum.los_for_chapter(chapter_code)) == expected


def test_one_lo_spans_several_chapters(curriculum):
    # Đây là thứ mã LO không nói được: "2" trong L.O.2.2 không phải số chương.
    assert list(curriculum.chapters_for_lo("L.O.2.2")) == ["4", "5", "7", "12"]
    assert list(curriculum.chapters_for_lo("L.O.2.1")) == ["6", "8", "9"]
    assert list(curriculum.chapters_for_lo("L.O.1.2")) == ["2", "3"]


def test_parent_lo_link_is_not_expanded_to_children(curriculum):
    # Chương 10 gắn L.O.3 (LO cha). Không được tự suy thành L.O.3.1 + L.O.3.2.
    assert list(curriculum.los_for_chapter("10")) == ["L.O.3"]
    assert curriculum.chapters_for_lo("L.O.3.1") == ()
    assert curriculum.chapters_for_lo("L.O.3.2") == ()


def test_chapter_lo_links_carry_provenance_and_source(curriculum):
    for link in curriculum.chapter_lo_links:
        assert link.provenance == "extracted"
        assert link.source is not None
        assert link.source.section == "6"
        assert link.source.page is not None


def test_row_scoped_lo_assessment_edges_keep_row_context(curriculum):
    # Chương 10 chỉ đánh giá L.O.3 qua A.O.1, dù mục 5.3 gắn cả A.O.2.
    row11 = {
        l.assessment_code
        for l in curriculum.lo_assessment_links
        if l.row_order_index == 11
    }
    assert row11 == {"A.O.1"}

    # Buổi 14 (trình bày dự án) là chỗ duy nhất trong mục 6 dùng A.O.1.1.
    row14 = {
        l.assessment_code
        for l in curriculum.lo_assessment_links
        if l.row_order_index == 14
    }
    assert row14 == {"A.O.1", "A.O.1.1"}


# ---------------------------------------------------------------------------
# Kiểm tra độ đầy đủ
# ---------------------------------------------------------------------------

def test_no_blocking_issue_on_a_well_formed_syllabus(curriculum):
    assert curriculum.blocking_issues == ()


def test_every_referenced_code_resolves(curriculum):
    lo_codes = set(curriculum.lo_by_code)
    ao_codes = set(curriculum.assessment_by_code)
    for link in curriculum.chapter_lo_links:
        assert link.lo_code in lo_codes
    for link in curriculum.lo_assessment_links:
        assert link.lo_code in lo_codes
        assert link.assessment_code in ao_codes


def test_leaf_lo_never_taught_is_surfaced(curriculum):
    # L.O.3.1 và L.O.3.2 được định nghĩa ở mục 4.2 nhưng không hàng nào của mục 6
    # nhắc tới. Đây là khoảng trống có thật trong tài liệu, phải hiện ra.
    not_taught = {
        i.message.split()[0]
        for i in curriculum.issues
        if i.code == "lo.not_taught"
    }
    assert not_taught == {"L.O.3.1", "L.O.3.2"}


def test_without_the_pdf_the_section_6_table_is_refused_not_guessed(tmp_path):
    """Không có bảng thì không có quan hệ chương–LO — báo lỗi, không đoán."""
    text_only = ParsedDocument(
        document=Document(
            id="text-only", name="dcmh.txt", path=None,
            doc_type=DocumentType.MARKDOWN, size_bytes=10, mime_type="text/plain",
        ),
        sections=[],
        page_count=1,
    )
    result = DcmhExtractor().extract(text_only, course_id_hint="CO3011")
    assert result.is_ok(), str(result.error)
    curriculum = result.unwrap()

    codes = {i.code for i in curriculum.blocking_issues}
    assert "session.table_missing" in codes
    assert curriculum.chapter_lo_links == ()
    assert curriculum.chapters == ()

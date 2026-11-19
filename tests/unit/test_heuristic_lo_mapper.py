"""
Tests cho HeuristicLoMapper.

Curriculum trong các test này được dựng để bẫy đúng lỗi cũ: mã LO và số chương
**cố tình lệch nhau**. L.O.2.2 được dạy ở chương 4, 5, 7, 12 và chương 2 lại dạy
L.O.1.2. Nếu mapper còn đọc số đầu của mã LO làm số chương thì mọi test dưới đây
đều sai chiều.
"""
import pytest

from document_chunk.adapters.curriculum.heuristic_lo_mapper import HeuristicLoMapper
from document_chunk.domain.entities.curriculum import (
    Chapter,
    ChapterLOLink,
    Course,
    Curriculum,
    LearningOutcome,
)
from document_chunk.domain.ports.metadata_store import StoredChunkMetadata

_CHAPTERS = {
    "2": "Đánh giá dự án và quản lý chương trình",
    "4": "Lựa chọn phương pháp tiếp cận dự án phù hợp",
    "7": "Quản lý rủi ro",
    "10": "Quản lý nhân sự trong môi trường phần mềm",
}

_LOS = {
    "L.O.1.2": "Nhận diện được các vấn đề phát sinh ảnh hưởng đến hiệu quả",
    "L.O.2.2": "Tổng hợp, phân tích, đánh giá, nhận diện vấn đề, tìm nguyên nhân",
    "L.O.3": "Giao tiếp hiệu quả trong nhóm, quản lý nguồn lực trong nhóm dự án",
}

# Đúng như đề cương CO3011 ghi ở mục 6.
_LINKS = [("2", "L.O.1.2"), ("4", "L.O.2.2"), ("7", "L.O.2.2"), ("10", "L.O.3")]


@pytest.fixture
def curriculum() -> Curriculum:
    return Curriculum(
        course=Course(course_id="CO3011", code="CO3011", title_vi="Quản lý Dự án Phần mềm"),
        chapters=tuple(
            Chapter(chapter_id=f"CO3011:CH{code}", code=code, title=title, order_index=int(code))
            for code, title in _CHAPTERS.items()
        ),
        learning_outcomes=tuple(
            LearningOutcome(
                lo_id=f"CO3011:{code}",
                code=code,
                parent_code=None,
                statement_vi=statement,
            )
            for code, statement in _LOS.items()
        ),
        assessments=(),
        chapter_lo_links=tuple(
            ChapterLOLink(chapter_code=ch, lo_code=lo) for ch, lo in _LINKS
        ),
    )


def _chunk(chunk_id: str, *heading: str) -> StoredChunkMetadata:
    return StoredChunkMetadata(
        chunk_id=chunk_id,
        document_id="doc-1",
        chunk_index=0,
        heading_path=heading,
        heading_level=len(heading),
        page_number=1,
        content_length=200,
        language="vi",
    )


def _lo_ids(mappings) -> set[str]:
    return {m.lo_id for m in mappings}


def test_chunk_in_chapter_7_maps_to_lo_2_2_not_lo_7(curriculum):
    """Chương 7 dạy L.O.2.2. Luật cũ sẽ đi tìm "L.O.7.*" và không ra gì."""
    result = HeuristicLoMapper().map([_chunk("c1", "Chương 7", "7.10 Kỹ thuật PERT")], curriculum)
    assert result.is_ok()
    assert _lo_ids(result.unwrap()) == {"CO3011:L.O.2.2"}


def test_chunk_in_chapter_2_does_not_map_to_lo_2_x(curriculum):
    """Chương 2 dạy L.O.1.2. Luật cũ sẽ gắn nhầm sang L.O.2.2."""
    result = HeuristicLoMapper().map([_chunk("c2", "Chương 2", "2.5 Chi phí lợi ích")], curriculum)
    assert result.is_ok()
    assert _lo_ids(result.unwrap()) == {"CO3011:L.O.1.2"}


def test_same_lo_reached_from_several_chapters(curriculum):
    result = HeuristicLoMapper().map(
        [_chunk("c3", "Chương 4"), _chunk("c4", "Chương 7")], curriculum
    )
    assert result.is_ok()
    assert {(m.chunk_id, m.lo_id) for m in result.unwrap()} == {
        ("c3", "CO3011:L.O.2.2"),
        ("c4", "CO3011:L.O.2.2"),
    }


def test_chapter_detected_by_title_when_number_absent(curriculum):
    result = HeuristicLoMapper().map([_chunk("c5", "Quản lý rủi ro", "Mô phỏng Monte Carlo")], curriculum)
    assert result.is_ok()
    assert _lo_ids(result.unwrap()) == {"CO3011:L.O.2.2"}


def test_textbook_subsection_number_is_not_a_chapter(curriculum):
    """"3.6 Bước 4: ..." là mục con trong giáo trình, không phải chương 3."""
    result = HeuristicLoMapper().map([_chunk("c6", "3.6 Bước 4: Xác định sản phẩm")], curriculum)
    assert result.is_ok()
    assert result.unwrap() == []


def test_mapping_stays_a_candidate_not_a_fact(curriculum):
    """Chương liên quan LO không chứng minh mọi chunk phục vụ LO đó."""
    result = HeuristicLoMapper().map([_chunk("c7", "Chương 10")], curriculum)
    assert result.is_ok()
    mapping = result.unwrap()[0]
    assert mapping.source == "chapter"
    assert mapping.confidence == pytest.approx(0.5)


def test_heading_matching_lo_wording_raises_confidence(curriculum):
    result = HeuristicLoMapper().map(
        [_chunk("c8", "Chương 10", "Giao tiếp hiệu quả trong nhóm dự án")], curriculum
    )
    assert result.is_ok()
    mapping = result.unwrap()[0]
    assert mapping.source == "chapter+heading"
    assert mapping.confidence == pytest.approx(0.7)


def test_no_chapter_lo_links_means_no_guessing(curriculum):
    """Đề cương chưa có quan hệ chương–LO thì không được tự bịa cạnh nào."""
    bare = Curriculum(
        course=curriculum.course,
        chapters=curriculum.chapters,
        learning_outcomes=curriculum.learning_outcomes,
        assessments=(),
        chapter_lo_links=(),
    )
    result = HeuristicLoMapper().map([_chunk("c9", "Chương 7")], bare)
    assert result.is_ok()
    assert result.unwrap() == []


def test_chunk_without_heading_is_skipped(curriculum):
    result = HeuristicLoMapper().map([_chunk("c10")], curriculum)
    assert result.is_ok()
    assert result.unwrap() == []

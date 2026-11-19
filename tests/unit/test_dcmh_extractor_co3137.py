"""
DcmhExtractor trên đề cương THẬT thứ hai (CO3137 Big Data, HK251) — bố cục mục 6
khác CO3011:

  - ô cột Buổi ghi số buổi ("1", "13, 14"), còn chương nằm ở dòng đầu ô Nội
    dung ("Chương 1. Giới thiệu", "Chương 14: Ôn tập");
  - mỗi buổi kéo dài 2–4 trang; từ trang 11, bảng mở đầu bằng một hàng rỗng
    trước hàng tiêu đề lặp lại;
  - mục 5.2 chỉ có A.O.2 và A.O.3 (không có A.O.1).

Sự thật nền (đọc tay từ file): 18 LO (4 cha, 14 con), 14 chương, 15 buổi mà
chương 13 chiếm buổi 13–14, chương 14 (Ôn tập) không gắn LO.
"""
from pathlib import Path

import pytest

from document_chunk.adapters.curriculum.dcmh_extractor import DcmhExtractor
from document_chunk.domain.entities.document import Document, DocumentType, ParsedDocument

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "syllabus" / "DCMH.CO3137.pdf"

_CHAPTER_TO_LO = {
    "1": ["L.O.1.1", "L.O.2.1"],
    "2": ["L.O.2.3", "L.O.4.3"],
    "3": ["L.O.1.2", "L.O.4.2"],
    "4": ["L.O.1.3", "L.O.4.3"],
    "5": ["L.O.1.1", "L.O.2.4", "L.O.3.1", "L.O.3.2", "L.O.3.3", "L.O.4.1", "L.O.4.2", "L.O.4.3"],
    "6": ["L.O.1.1", "L.O.2.1", "L.O.4.1", "L.O.4.2"],
    "7": ["L.O.2.2", "L.O.2.4", "L.O.3.1", "L.O.3.2", "L.O.3.3", "L.O.4.2", "L.O.4.3"],
    "8": ["L.O.2.2", "L.O.2.3", "L.O.2.4", "L.O.3.3", "L.O.4.1"],
    "9": ["L.O.1.2", "L.O.4.2"],
    "10": ["L.O.1.4", "L.O.2.1", "L.O.4.3"],
    "11": ["L.O.1.4", "L.O.2.1", "L.O.4.3"],
    "12": ["L.O.1.1", "L.O.2.1", "L.O.4.3"],
    "13": ["L.O.2.1", "L.O.2.2", "L.O.2.3", "L.O.3.1", "L.O.3.2", "L.O.3.3", "L.O.4.1", "L.O.4.2", "L.O.4.3"],
    "14": [],
}


@pytest.fixture(scope="module")
def curriculum():
    if not _FIXTURE.exists():  # pragma: no cover
        pytest.skip(f"thiếu fixture đề cương: {_FIXTURE}")
    parsed = ParsedDocument(
        document=Document(
            id="dcmh-co3137",
            name=_FIXTURE.name,
            path=_FIXTURE,
            doc_type=DocumentType.PDF,
            size_bytes=_FIXTURE.stat().st_size,
            mime_type="application/pdf",
        ),
        sections=[],
        page_count=35,
    )
    result = DcmhExtractor().extract(parsed, course_id_hint="CO3137")
    assert result.is_ok(), str(result.error)
    return result.unwrap()


def test_course_header(curriculum):
    course = curriculum.course
    assert course.code == "CO3137"
    assert course.title_vi == "Dữ liệu lớn"
    assert course.title_en == "Big Data"
    assert course.credits == 3
    assert course.semester == "HK251"
    assert course.syllabus_version == "DCMH.CO3137.3.1"


def test_learning_outcome_tree(curriculum):
    los = curriculum.learning_outcomes
    assert len(los) == 18
    assert [lo.code for lo in los if lo.parent_code is None] == ["L.O.1", "L.O.2", "L.O.3", "L.O.4"]


def test_chapters_come_from_the_content_cell(curriculum):
    chapters = {ch.code: ch for ch in curriculum.chapters}
    assert list(chapters) == [str(n) for n in range(1, 15)]
    assert chapters["1"].title == "Giới thiệu"
    assert chapters["1"].title_en == "Introduction"
    # Tên xuống dòng trong ô vẫn phải đủ.
    assert chapters["6"].title == "Giải thuật xử lý dữ liệu dòng dùng cấu trúc dữ liệu xác suất"
    # "Chương 14: Ôn tập" dùng dấu hai chấm.
    assert chapters["14"].title == "Ôn tập"


def test_every_page_of_section_6_is_read(curriculum):
    """Bảng từ trang 11 mở đầu bằng hàng rỗng; bỏ sót chúng thì chỉ còn buổi 1."""
    links: dict[str, list[str]] = {code: [] for code in _CHAPTER_TO_LO}
    for link in curriculum.chapter_lo_links:
        links[link.chapter_code].append(link.lo_code)
    assert {code: sorted(los) for code, los in links.items()} == _CHAPTER_TO_LO


def test_chapter_spanning_two_sessions(curriculum):
    sessions = [(row.session_no, row.chapter_code) for row in curriculum.sessions]
    assert sessions[-2:] == [(13, "13"), (15, "14")]
    assert not [i for i in curriculum.issues if i.code == "session.number_mismatch"]


def test_assessments_without_a_o_1(curriculum):
    assert [a.code for a in curriculum.assessments] == ["A.O.2", "A.O.3"]


def test_nothing_blocks_the_import(curriculum):
    assert curriculum.blocking_issues == ()

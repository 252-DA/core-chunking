"""
Unit tests for DcmhExtractor — structural-only parser.

Uses a synthetic DCMH text that mimics CO3115 CDIO format.
"""
import pytest

from document_chunk.adapters.curriculum.dcmh_extractor import DcmhExtractor
from document_chunk.domain.entities.document import Document, DocumentType, ElementType, ParsedDocument, Section
from document_chunk.shared.result import Ok


_SYNTHETIC_DCMH = """\
TRƯỜNG ĐẠI HỌC BÁCH KHOA TP.HCM
Khoa Khoa học và Kỹ thuật Máy tính

ĐỀ CƯƠNG MÔN HỌC

Tên học phần (Tiếng Việt): Phân tích và Thiết kế Hệ thống
Tên học phần (Tiếng Anh): Systems Analysis and Design
Mã học phần (Course ID): CO3115
Số tín chỉ: 3
Học kỳ: 1

4.2 Chuẩn đầu ra học phần

L.O.1 - Hiểu được các khái niệm cơ bản về phân tích hệ thống
(Understand the basic concepts of systems analysis)

L.O.2 - Hiểu được quy trình phát triển phần mềm
(Understand the software development process)

L.O.3 - Phân tích yêu cầu hệ thống
(Analyze system requirements)

L.O.3.1 - Phân tích yêu cầu chức năng
(Analyze functional requirements)

L.O.3.2 - Phân tích yêu cầu phi chức năng
(Analyze non-functional requirements)

L.O.4 - Thiết kế hệ thống thông tin
(Design information systems)

L.O.4.1 - Thiết kế cơ sở dữ liệu
(Design the database schema)

L.O.4.2 - Thiết kế kiến trúc hệ thống
(Design the system architecture)

L.O.5 - Áp dụng các công cụ CASE
(Apply CASE tools in practice)

5.2 Phương pháp đánh giá

A.O.1 - Bài kiểm tra nhanh (Quiz): 10%
A.O.2 - Kiểm tra giữa kỳ (Midterm): 30%
A.O.3 - Kiểm tra cuối kỳ (Final): 60%

5.3 Liên kết LO - Đánh giá

L.O.3.1 ... A.O.2
L.O.3.2 ... A.O.2
L.O.4.1 ... A.O.3
L.O.4.2 ... A.O.3
L.O.1 ... A.O.1
L.O.2 ... A.O.1

6. Nội dung chi tiết

1. Giới thiệu môn học
2. Quy trình phát triển phần mềm
3. Phân tích yêu cầu
4. Thiết kế hệ thống
"""


def _make_parsed_doc(text: str) -> ParsedDocument:
    doc = Document(
        id="test-doc",
        name="DCMH.CO3115.pdf",
        path=None,
        doc_type=DocumentType.PDF,
        size_bytes=len(text),
        mime_type="application/pdf",
    )
    return ParsedDocument(
        document=doc,
        sections=[Section(content=text, element_type=ElementType.PARAGRAPH)],
        page_count=5,
        language="vi",
    )


@pytest.fixture
def extractor() -> DcmhExtractor:
    return DcmhExtractor()


@pytest.fixture
def parsed_co3115() -> ParsedDocument:
    return _make_parsed_doc(_SYNTHETIC_DCMH)


def test_extract_course_header(extractor, parsed_co3115):
    result = extractor.extract(parsed_co3115, course_id_hint="CO3115")
    assert result.is_ok(), str(result.error)
    curriculum = result.unwrap()
    assert curriculum.course.code == "CO3115"
    assert curriculum.course.credits == 3
    assert "Phân tích" in curriculum.course.title_vi


def test_extract_learning_outcomes(extractor, parsed_co3115):
    curriculum = extractor.extract(parsed_co3115, course_id_hint="CO3115").unwrap()
    lo_codes = {lo.code for lo in curriculum.learning_outcomes}
    # Parent LOs
    assert "L.O.1" in lo_codes
    assert "L.O.3" in lo_codes
    assert "L.O.4" in lo_codes
    # Child LOs
    assert "L.O.3.1" in lo_codes
    assert "L.O.3.2" in lo_codes
    assert "L.O.4.1" in lo_codes
    assert "L.O.4.2" in lo_codes
    # At least 7 LOs total
    assert len(curriculum.learning_outcomes) >= 7


def test_lo_ids_prefixed_with_course(extractor, parsed_co3115):
    curriculum = extractor.extract(parsed_co3115, course_id_hint="CO3115").unwrap()
    for lo in curriculum.learning_outcomes:
        assert lo.lo_id.startswith("CO3115:"), f"Bad lo_id: {lo.lo_id}"


def test_parent_child_lo_relationship(extractor, parsed_co3115):
    curriculum = extractor.extract(parsed_co3115, course_id_hint="CO3115").unwrap()
    lo_by_code = {lo.code: lo for lo in curriculum.learning_outcomes}
    lo_3_1 = lo_by_code.get("L.O.3.1")
    assert lo_3_1 is not None
    assert lo_3_1.parent_code == "L.O.3"


def test_extract_assessments(extractor, parsed_co3115):
    curriculum = extractor.extract(parsed_co3115, course_id_hint="CO3115").unwrap()
    assert len(curriculum.assessments) >= 3
    codes = {a.code for a in curriculum.assessments}
    assert "A.O.1" in codes
    assert "A.O.2" in codes
    assert "A.O.3" in codes


def test_assessment_category_inference(extractor, parsed_co3115):
    curriculum = extractor.extract(parsed_co3115, course_id_hint="CO3115").unwrap()
    by_code = {a.code: a for a in curriculum.assessments}
    assert by_code["A.O.2"].category == "midterm"
    assert by_code["A.O.3"].category == "final"


def test_lo_assessment_links(extractor, parsed_co3115):
    curriculum = extractor.extract(parsed_co3115, course_id_hint="CO3115").unwrap()
    assert len(curriculum.lo_assessment_links) >= 2


def test_extract_chapters(extractor, parsed_co3115):
    curriculum = extractor.extract(parsed_co3115, course_id_hint="CO3115").unwrap()
    assert len(curriculum.chapters) >= 2


def test_extraction_confidence_above_threshold(extractor, parsed_co3115):
    curriculum = extractor.extract(parsed_co3115, course_id_hint="CO3115").unwrap()
    assert curriculum.extraction_confidence >= 0.8


def test_bloom_level_inferred(extractor, parsed_co3115):
    curriculum = extractor.extract(parsed_co3115, course_id_hint="CO3115").unwrap()
    by_code = {lo.code: lo for lo in curriculum.learning_outcomes}
    lo_3 = by_code.get("L.O.3")
    assert lo_3 is not None
    assert lo_3.bloom_level == "analyze"


def test_error_on_no_course_code(extractor):
    bad_text = "Some text without a course code"
    parsed = _make_parsed_doc(bad_text)
    result = extractor.extract(parsed)
    assert result.is_err()

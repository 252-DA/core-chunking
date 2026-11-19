"""
Độ đúng và độ đầy đủ của đầu vào PDF — đánh giá theo từng trang.

Yêu cầu được kiểm tra ở đây: tài liệu có trang scan thì những trang đó phải
được ghi nhận và xử lý, hoặc phần chưa đọc được phải báo rõ theo số trang.
Không trả "parse thành công" chỉ vì các trang còn lại có text.
"""
from unittest.mock import patch

import fitz
import pytest

from document_chunk.adapters.parsers.adaptive_pdf_parser import AdaptivePdfParser
from document_chunk.adapters.parsers.pdf_parser import PdfParser
from document_chunk.domain.entities.document import (
    Document,
    DocumentType,
    ElementType,
    ParsedDocument,
    Section,
)
from document_chunk.domain.exceptions import (
    IncompletePageCoverageError,
    ParseBudgetExceededError,
)
from document_chunk.infrastructure.config import ParserConfig
from document_chunk.shared.result import Err, Ok

BODY = (
    "He quan tri co so du lieu la phan mem quan ly du lieu co cau truc, "
    "ho tro truy van va bao dam tinh toan ven du lieu trong he thong."
)


# ---------------------------------------------------------------------------
# PDF fixtures dựng bằng PyMuPDF
# ---------------------------------------------------------------------------

def _text_page(pdf: fitz.Document, number: int) -> None:
    page = pdf.new_page()
    page.insert_text((72, 30), f"Giao trinh CSDL - {number}")
    page.insert_textbox(fitz.Rect(72, 90, 520, 300), BODY, fontsize=11)


def _scan_page(pdf: fitz.Document, number: int) -> None:
    """Trang scan: chỉ có ảnh phủ kín, text layer không có gì dùng được."""
    page = pdf.new_page()
    pixmap = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 400, 560), False)
    pixmap.clear_with(210)
    page.insert_image(page.rect, pixmap=pixmap)
    page.insert_text((72, 30), f"Giao trinh CSDL - {number}")


def _two_column_page(pdf: fitz.Document, number: int) -> None:
    page = pdf.new_page()
    page.insert_text((72, 30), f"Giao trinh CSDL - {number}")
    page.insert_textbox(fitz.Rect(60, 90, 280, 500), BODY * 2, fontsize=10)
    page.insert_textbox(fitz.Rect(320, 90, 540, 500), BODY * 2, fontsize=10)


def _table_page(pdf: fitz.Document, number: int) -> None:
    page = pdf.new_page()
    page.insert_text((72, 30), f"Giao trinh CSDL - {number}")
    page.insert_textbox(fitz.Rect(72, 90, 520, 160), BODY, fontsize=11)
    xs = [72, 220, 380, 520]
    ys = [200, 240, 280, 320]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    rows = [["Ten", "Kieu", "Mo ta"], ["id", "int", "khoa chinh"], ["ten", "text", "ho ten"]]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((xs[c] + 5, ys[r] + 25), cell, fontsize=9)


def _build(tmp_path, name, builders):
    path = tmp_path / name
    with fitz.open() as pdf:
        for index, builder in enumerate(builders, start=1):
            builder(pdf, index)
        pdf.save(path)
    return path


def _mixed_pdf(tmp_path):
    """4 trang text + 1 trang scan — tỉ lệ cần fallback 1/5."""
    return _build(
        tmp_path,
        "mixed.pdf",
        [_text_page, _text_page, _scan_page, _text_page, _text_page],
    )


def _entry(parsed, page):
    return next(e for e in parsed.metadata["page_report"] if e["page"] == page)


# ---------------------------------------------------------------------------
# Đánh giá từng trang
# ---------------------------------------------------------------------------

def test_scan_page_is_recorded_not_hidden_by_text_pages(tmp_path):
    path = _mixed_pdf(tmp_path)

    parsed = PdfParser(ParserConfig(), enforce_coverage=False).parse(path).unwrap()

    assert _entry(parsed, 3)["status"] == "needs_ocr"
    assert _entry(parsed, 3)["parsed_by"] is None
    assert _entry(parsed, 3)["reasons"] == ["image_page"]
    assert [e["status"] for e in parsed.metadata["page_report"]].count("text") == 4

    coverage = parsed.metadata["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["unprocessed_pages"] == [3]
    assert coverage["pages_resolved"] == 4


def test_strict_parser_refuses_to_report_success_with_unread_pages(tmp_path):
    path = _mixed_pdf(tmp_path)

    result = PdfParser(ParserConfig()).parse(path)

    assert result.is_err()
    error = result.error
    assert isinstance(error, IncompletePageCoverageError)
    assert error.pages == [3]
    assert "trang 3" in str(error)
    # Report vẫn đi kèm để caller kiểm tra được phần đã đọc.
    assert error.parsed.metadata["coverage"]["pages_resolved"] == 4


def test_multi_column_page_is_flagged_for_layout_backend(tmp_path):
    path = _build(tmp_path, "columns.pdf", [_text_page, _two_column_page, _text_page])

    parsed = PdfParser(ParserConfig(), enforce_coverage=False).parse(path).unwrap()

    entry = _entry(parsed, 2)
    assert entry["status"] == "complex_layout"
    assert entry["multi_column"] is True
    assert "multi_column" in entry["reasons"]
    # Có text nên không tính là mất dữ liệu, nhưng thứ tự đọc chưa đảm bảo.
    assert parsed.metadata["coverage"]["unprocessed_pages"] == []
    assert parsed.metadata["coverage"]["degraded_pages"] == [2]


def test_table_page_is_flagged_for_layout_backend(tmp_path):
    path = _build(tmp_path, "table.pdf", [_text_page, _table_page])

    parsed = PdfParser(ParserConfig(), enforce_coverage=False).parse(path).unwrap()

    entry = _entry(parsed, 2)
    assert entry["status"] == "complex_layout"
    assert entry["tables"] >= 1
    assert "table_detected" in entry["reasons"]


def test_blank_page_is_not_a_failure(tmp_path):
    def blank(pdf, number):
        pdf.new_page()

    path = _build(tmp_path, "blank.pdf", [_text_page, blank])

    parsed = PdfParser(ParserConfig()).parse(path).unwrap()

    assert _entry(parsed, 2)["status"] == "empty"
    assert parsed.metadata["coverage"]["unprocessed_pages"] == []


def test_sections_keep_page_number_and_source_position(tmp_path):
    path = _build(tmp_path, "one.pdf", [_text_page])

    parsed = PdfParser(ParserConfig()).parse(path).unwrap()

    section = parsed.sections[0]
    assert section.page_number == 1
    source = section.metadata["source"]
    assert source["parser"] == "pymupdf"
    assert source["page"] == 1
    assert len(source["bbox"]) == 4


# ---------------------------------------------------------------------------
# Ngân sách xử lý theo tài liệu
# ---------------------------------------------------------------------------

def test_image_bytes_are_not_extracted_by_default(tmp_path):
    path = _build(tmp_path, "scan.pdf", [_scan_page, _text_page])

    parsed = PdfParser(ParserConfig(), enforce_coverage=False).parse(path).unwrap()

    assert parsed.images == {}
    assert parsed.metadata["budget"]["extract_images"] is False


def test_image_extraction_respects_byte_budget(tmp_path):
    path = _build(tmp_path, "scan.pdf", [_scan_page, _text_page])
    config = ParserConfig(pdf_extract_images=True)

    parsed = PdfParser(config, enforce_coverage=False).parse(path).unwrap()
    assert parsed.images
    assert parsed.metadata["budget"]["images_truncated"] is False

    capped = ParserConfig(pdf_extract_images=True, pdf_max_image_bytes=16)
    parsed = PdfParser(capped, enforce_coverage=False).parse(path).unwrap()
    assert parsed.images == {}
    assert parsed.metadata["budget"]["images_truncated"] is True


def test_file_size_budget_is_reported_as_budget_error(tmp_path):
    path = _build(tmp_path, "one.pdf", [_text_page])

    result = PdfParser(ParserConfig(pdf_max_file_bytes=10)).parse(path)

    assert result.is_err()
    assert isinstance(result.error, ParseBudgetExceededError)
    assert result.error.limit == "pdf_max_file_bytes"


def test_time_budget_is_reported_as_budget_error(tmp_path):
    path = _build(tmp_path, "one.pdf", [_text_page])

    result = PdfParser(ParserConfig(pdf_timeout_seconds=0.0)).parse(path)

    assert result.is_err()
    assert isinstance(result.error, ParseBudgetExceededError)
    assert result.error.limit == "pdf_timeout_seconds"


def test_page_limit_is_recorded_as_skipped_not_silently_dropped(tmp_path):
    path = _build(tmp_path, "four.pdf", [_text_page] * 4)

    parsed = PdfParser(ParserConfig(pdf_max_pages=2)).parse(path).unwrap()

    coverage = parsed.metadata["coverage"]
    assert coverage["pages_total"] == 4
    assert coverage["pages_examined"] == 2
    assert coverage["pages_skipped_by_limit"] == [[3, 4]]
    assert coverage["status"] == "partial"


# ---------------------------------------------------------------------------
# Routing theo trang + ghép kết quả
# ---------------------------------------------------------------------------

class _FakeDocling:
    """Docling giả — không nạp model OCR/layout trong unit test."""

    def __init__(self, sections_by_page=None, full_sections=None, fail=False):
        self._sections_by_page = sections_by_page or {}
        self._full_sections = full_sections or []
        self._fail = fail
        self.range_calls: list[tuple[int, int]] = []
        self.full_calls = 0

    def _parsed(self, path, sections, page_count):
        return ParsedDocument(
            document=Document(
                id="fake", name=path.name, path=path, doc_type=DocumentType.PDF,
                size_bytes=path.stat().st_size, mime_type="application/pdf",
            ),
            sections=sections,
            page_count=page_count,
            metadata={"parser": "docling", "page_report": []},
        )

    def parse_pages(self, path, first, last):
        self.range_calls.append((first, last))
        if self._fail:
            return Err(RuntimeError("docling range failed"))
        sections = [
            section
            for page in range(first, last + 1)
            for section in self._sections_by_page.get(page, [])
        ]
        return Ok(self._parsed(path, sections, last))

    def parse(self, path):
        self.full_calls += 1
        if self._fail:
            return Err(RuntimeError("docling failed"))
        return Ok(self._parsed(path, self._full_sections, 5))


def _ocr_section(page: int, text: str) -> Section:
    return Section(
        content=text,
        element_type=ElementType.PARAGRAPH,
        page_number=page,
        metadata={"docling_type": "TextItem"},
    )


@patch("document_chunk.adapters.parsers.adaptive_pdf_parser.DoclingPdfParser")
def test_only_pending_pages_go_through_ocr(docling_cls, tmp_path):
    path = _mixed_pdf(tmp_path)
    fake = _FakeDocling(sections_by_page={3: [_ocr_section(3, "Noi dung trang scan")]})
    docling_cls.return_value = fake

    result = AdaptivePdfParser(ParserConfig()).parse(path)

    assert result.is_ok()
    parsed = result.unwrap()
    # Chỉ trang scan được OCR, không OCR lại cả tài liệu.
    assert fake.range_calls == [(3, 3)]
    assert fake.full_calls == 0

    assert _entry(parsed, 3)["parsed_by"] == "docling"
    coverage = parsed.metadata["coverage"]
    assert coverage["status"] == "complete"
    assert coverage["unprocessed_pages"] == []
    assert coverage["by_backend"] == {"pymupdf": 4, "docling": 1}

    # Nội dung OCR nằm đúng vị trí trang 3 trong thứ tự sections.
    pages = [s.page_number for s in parsed.sections]
    assert pages == sorted(pages)
    ocr = [s for s in parsed.sections if s.content == "Noi dung trang scan"]
    assert len(ocr) == 1
    assert ocr[0].metadata["source"] == {
        "parser": "docling", "page": 3, "element": "TextItem"
    }


@patch("document_chunk.adapters.parsers.adaptive_pdf_parser.DoclingPdfParser")
def test_ocr_without_text_is_recorded_as_processed_not_missing(docling_cls, tmp_path):
    path = _mixed_pdf(tmp_path)
    fake = _FakeDocling(sections_by_page={})   # OCR chạy nhưng trang không có chữ
    docling_cls.return_value = fake

    result = AdaptivePdfParser(ParserConfig()).parse(path)

    assert result.is_ok()
    coverage = result.unwrap().metadata["coverage"]
    assert fake.range_calls == [(3, 3)]
    assert coverage["no_text_pages"] == [3]      # đã xử lý, không có text
    assert coverage["unprocessed_pages"] == []   # không phải "chưa ai đọc"
    assert coverage["status"] == "partial"


@patch("document_chunk.adapters.parsers.adaptive_pdf_parser.DoclingPdfParser")
def test_failed_ocr_leaves_pages_unread_and_errors(docling_cls, tmp_path):
    path = _mixed_pdf(tmp_path)
    fake = _FakeDocling(fail=True)
    docling_cls.return_value = fake

    result = AdaptivePdfParser(ParserConfig()).parse(path)

    assert result.is_err()
    assert isinstance(result.error, IncompletePageCoverageError)
    assert result.error.pages == [3]


@patch("document_chunk.adapters.parsers.adaptive_pdf_parser.DoclingPdfParser")
def test_disabled_fallback_still_reports_unread_pages(docling_cls, tmp_path):
    path = _mixed_pdf(tmp_path)

    result = AdaptivePdfParser(ParserConfig(pdf_page_fallback_enabled=False)).parse(path)

    assert docling_cls.call_count == 0
    assert result.is_err()
    assert result.error.pages == [3]


@patch("document_chunk.adapters.parsers.adaptive_pdf_parser.DoclingPdfParser")
def test_non_strict_mode_returns_partial_result_with_report(docling_cls, tmp_path):
    path = _mixed_pdf(tmp_path)
    config = ParserConfig(pdf_page_fallback_enabled=False, pdf_strict_missing_text=False)

    result = AdaptivePdfParser(config).parse(path)

    assert result.is_ok()
    coverage = result.unwrap().metadata["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["unprocessed_pages"] == [3]


@patch("document_chunk.adapters.parsers.adaptive_pdf_parser.DoclingPdfParser")
def test_mostly_scanned_document_runs_one_full_conversion(docling_cls, tmp_path):
    path = _build(
        tmp_path, "scans.pdf", [_scan_page, _scan_page, _scan_page, _text_page]
    )
    fake = _FakeDocling(
        full_sections=[_ocr_section(page, f"Trang {page}") for page in range(1, 5)]
    )
    docling_cls.return_value = fake

    result = AdaptivePdfParser(ParserConfig()).parse(path)

    assert result.is_ok()
    assert fake.full_calls == 1
    assert fake.range_calls == []
    assert result.unwrap().metadata["coverage"]["unprocessed_pages"] == []


@patch("document_chunk.adapters.parsers.adaptive_pdf_parser.DoclingPdfParser")
def test_text_document_never_loads_the_ocr_backend(docling_cls, tmp_path):
    path = _build(tmp_path, "text.pdf", [_text_page, _text_page])

    result = AdaptivePdfParser(ParserConfig()).parse(path)

    assert result.is_ok()
    assert docling_cls.call_count == 0
    assert result.unwrap().metadata["coverage"]["status"] == "complete"


@patch("document_chunk.adapters.parsers.adaptive_pdf_parser.DoclingPdfParser")
def test_page_limit_also_caps_the_expensive_backend(docling_cls, tmp_path):
    path = _build(tmp_path, "scans.pdf", [_scan_page] * 4)
    fake = _FakeDocling(full_sections=[_ocr_section(1, "Trang 1"), _ocr_section(2, "Trang 2")])
    docling_cls.return_value = fake

    config = ParserConfig(pdf_max_pages=2, pdf_strict_missing_text=False)
    result = AdaptivePdfParser(config).parse(path)

    assert result.is_ok()
    assert fake.full_calls == 0
    assert fake.range_calls == [(1, 2)]   # không OCR 2 trang ngoài hạn mức
    coverage = result.unwrap().metadata["coverage"]
    assert coverage["pages_total"] == 4
    assert coverage["pages_examined"] == 2
    assert coverage["pages_skipped_by_limit"] == [[3, 4]]

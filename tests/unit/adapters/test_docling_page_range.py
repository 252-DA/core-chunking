"""
Docling chạy theo khoảng trang — điều kiện để chỉ OCR những trang thiếu text.

Test không nạp model OCR/layout: converter được thay bằng stub, phần kiểm tra
là plumbing page_range và cách số trang tuyệt đối được giữ trong page report.
"""
import fitz
from docling_core.types.doc.base import BoundingBox, CoordOrigin
from docling_core.types.doc.document import ProvenanceItem, TextItem
from docling_core.types.doc.labels import DocItemLabel

from document_chunk.adapters.parsers.docling_pdf_parser import DoclingPdfParser
from document_chunk.infrastructure.config import ParserConfig


def _text_item(page: int, text: str, index: int = 0) -> TextItem:
    prov = ProvenanceItem(
        page_no=page,
        bbox=BoundingBox(l=0, t=0, r=100, b=20, coord_origin=CoordOrigin.TOPLEFT),
        charspan=(0, len(text)),
    )
    return TextItem(
        self_ref=f"#/texts/{index}",
        label=DocItemLabel.TEXT,
        orig=text,
        text=text,
        prov=[prov],
    )


class _FakeDoclingDocument:
    def __init__(self, items, num_pages):
        self._items = items
        self._num_pages = num_pages

    def iterate_items(self):
        return [(item, 0) for item in self._items]

    def num_pages(self):
        return self._num_pages


class _FakeResult:
    def __init__(self, items, num_pages):
        self.document = _FakeDoclingDocument(items, num_pages)
        self.pages = list(range(num_pages))


class _FakeConverter:
    def __init__(self, items, num_pages):
        self._items = items
        self._num_pages = num_pages
        self.calls = []

    def convert(self, source, **kwargs):
        self.calls.append((source, kwargs))
        return _FakeResult(self._items, self._num_pages)


def _pdf(tmp_path, pages=5):
    path = tmp_path / "doc.pdf"
    with fitz.open() as pdf:
        for index in range(pages):
            pdf.new_page().insert_text((72, 100), f"trang {index + 1}")
        pdf.save(path)
    return path


def _parser(tmp_path, items, num_pages=5, **config):
    parser = DoclingPdfParser(ParserConfig(**config))
    converter = _FakeConverter(items, num_pages)
    parser.__dict__["_converter"] = converter   # bỏ qua lazy load model
    return parser, converter


def test_parse_pages_forwards_the_page_range(tmp_path):
    path = _pdf(tmp_path)
    parser, converter = _parser(tmp_path, [_text_item(3, "Noi dung trang ba")])

    parsed = parser.parse_pages(path, 3, 4).unwrap()

    assert converter.calls == [(str(path), {"page_range": (3, 4)})]
    assert parsed.metadata["page_range"] == [3, 4]
    # Số trang giữ nguyên là số trang tuyệt đối trong tài liệu.
    assert [s.page_number for s in parsed.sections] == [3]


def test_range_report_separates_read_pages_from_unread_ones(tmp_path):
    path = _pdf(tmp_path)
    parser, _ = _parser(tmp_path, [_text_item(3, "Noi dung trang ba")])

    parsed = parser.parse_pages(path, 3, 4).unwrap()

    report = {entry["page"]: entry for entry in parsed.metadata["page_report"]}
    assert set(report) == {3, 4}
    assert report[3]["parsed_by"] == "docling"
    assert report[3]["layout_aware"] is True
    assert report[4]["parsed_by"] is None
    assert report[4]["reasons"] == ["no_content_from_docling"]


def test_full_parse_does_not_pass_a_page_range(tmp_path):
    path = _pdf(tmp_path)
    parser, converter = _parser(
        tmp_path, [_text_item(page, f"trang {page}", page) for page in range(1, 6)]
    )

    parsed = parser.parse(path).unwrap()

    assert converter.calls == [(str(path), {})]
    assert parsed.metadata["page_range"] is None
    assert len(parsed.metadata["page_report"]) == 5


def test_image_bytes_stay_off_unless_enabled(tmp_path):
    path = _pdf(tmp_path)
    parser, _ = _parser(tmp_path, [_text_item(1, "chi co text")])

    parsed = parser.parse(path).unwrap()

    assert parsed.images == {}
    assert parsed.metadata["extract_images"] is False

"""
PDF xuất từ slide: mỗi trang thành một Section SLIDE, tiêu đề là chữ lớn nhất trang.

Lỗi được chặn ở đây: đoán heading theo cỡ chữ trên cả file biến mọi dòng in to
hoặc in đậm trong slide thành một mục, và chân trang của template lọt vào nội dung.
"""
from unittest.mock import patch

import fitz

from document_chunk.adapters.chunkers.heading_chunker import HeadingChunker
from document_chunk.adapters.parsers.adaptive_pdf_parser import AdaptivePdfParser
from document_chunk.adapters.parsers.pdf_parser import PdfParser
from document_chunk.adapters.parsers.pdf_slides import _join, fold_into_slide, slide_section
from document_chunk.domain.entities.document import (
    Document,
    DocumentType,
    ElementType,
    ParsedDocument,
    Section,
)
from document_chunk.infrastructure.config import ChunkerConfig, ParserConfig
from document_chunk.shared.result import Ok

WIDTH, HEIGHT = 960, 540
FOOTERS = ["HPC Lab - CSE - HCMUT, 2024", "HPC Lab-CSE-HCMUT"]


def _slide(pdf, number, title_lines=(), bullets=(), title_y=70, extra=None):
    page = pdf.new_page(width=WIDTH, height=HEIGHT)
    # Nhãn template nằm ngoài dải lề nên PdfParser không tự bỏ.
    page.insert_text((20, 60), FOOTERS[number % 2], fontsize=11)
    for i, line in enumerate(title_lines):
        page.insert_text((60, title_y + i * 42), line, fontsize=36)
    for i, line in enumerate(bullets):
        page.insert_text((80, 170 + i * 36), line, fontsize=20)
    if extra:
        extra(page)
    return page


def _deck(tmp_path, scan_page=False):
    path = tmp_path / "deck.pdf"
    with fitz.open() as pdf:
        _slide(pdf, 1, ["Finding similar items in massive datasets"], title_y=270)
        _slide(
            pdf,
            2,
            ["Min-Hashing"],
            [
                "• Pick a random permutation of the rows of the matrix",
                "• The min-hash of a column is the first row with a one",
            ],
            extra=lambda page: page.insert_text((80, 300), "Example", fontsize=24, fontname="hebo"),
        )
        if scan_page:
            page = _slide(pdf, 3, ["Data sources"])
            pixmap = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 480, 270), False)
            pixmap.clear_with(210)
            page.insert_image(fitz.Rect(0, 120, WIDTH, HEIGHT), pixmap=pixmap)
        else:
            _slide(
                pdf,
                3,
                ["Similarity of", "signatures"],
                ["• Signatures agree on the fraction of hash functions that collide"],
            )
        _slide(
            pdf,
            4,
            bullets=[
                "• The similarity of two signatures is the fraction of hash functions",
                "• in which they agree, which estimates the Jaccard similarity",
            ],
        )
        _slide(pdf, 5, ["Banding"], ["• Divide the signature matrix into b bands of r rows"])
        pdf.save(path)
    return path


def _parse(path, mode="auto"):
    return PdfParser(ParserConfig(pdf_slide_mode=mode), enforce_coverage=False).parse(path).unwrap()


def test_slide_pdf_becomes_one_slide_per_page(tmp_path):
    parsed = _parse(_deck(tmp_path))

    assert parsed.metadata["layout"] == "slides"
    assert parsed.is_slides
    assert [s.element_type for s in parsed.sections] == [ElementType.SLIDE] * 5
    assert [s.page_number for s in parsed.sections] == [1, 2, 3, 4, 5]
    # Tiêu đề là chữ lớn nhất trang; "Example" in đậm chỉ là chữ trong slide.
    # Trang 4 không có chữ nổi bật: slide tiếp nối, không bịa tiêu đề.
    assert [s.heading for s in parsed.sections] == [
        "Finding similar items in massive datasets",
        "Min-Hashing",
        "Similarity of signatures",
        None,
        "Banding",
    ]
    assert "Example" in parsed.sections[1].content


def test_slide_blocks_keep_bullets_and_drop_template_labels(tmp_path):
    parsed = _parse(_deck(tmp_path))

    min_hash = parsed.sections[1]
    assert [b["kind"] for b in min_hash.metadata["blocks"]][:2] == ["list", "list"]
    assert min_hash.metadata["blocks"][0]["text"].startswith("• Pick a random permutation")
    assert all("HCMUT" not in s.content for s in parsed.sections)
    # Slide chỉ có tiêu đề vẫn có nội dung để index.
    assert parsed.sections[0].content == "Finding similar items in massive datasets"


def test_slide_mode_is_off_by_default(tmp_path):
    parsed = PdfParser(ParserConfig(), enforce_coverage=False).parse(_deck(tmp_path)).unwrap()

    assert parsed.metadata["layout"] == "flow"
    assert not parsed.is_slides
    assert ElementType.SLIDE not in {s.element_type for s in parsed.sections}


def test_portrait_text_document_is_not_treated_as_slides(tmp_path):
    path = tmp_path / "paper.pdf"
    body = "Lakehouse systems store data in open formats on low cost object storage. " * 20
    with fitz.open() as pdf:
        for _ in range(3):
            page = pdf.new_page()  # A4 dọc
            page.insert_text((72, 60), "Introduction", fontsize=16)
            page.insert_textbox(fitz.Rect(72, 90, 520, 760), body, fontsize=10)
        pdf.save(path)

    assert _parse(path).metadata["layout"] == "flow"


def test_line_break_hyphen_is_joined():
    assert _join(["Divide the signature ma-", "trix into bands"]) == "Divide the signature matrix into bands"
    assert _join(["Min-", "Hash"]) == "Min- Hash"


def test_ocr_sections_fold_into_the_slide_and_keep_its_title():
    text_slide = slide_section(3, "Data sources", [], source={"page": 3})
    ocr = [
        Section("Data sources", ElementType.HEADING, "Data sources", 1, page_number=3),
        Section("LHC generates 15 PB a year", ElementType.PARAGRAPH, page_number=3),
        Section("[Image]", ElementType.IMAGE, page_number=3),
        Section("| a | b |\n| --- | --- |\n| 1 | 2 |", ElementType.TABLE, page_number=3),
    ]

    folded = fold_into_slide(3, ocr, text_slide)

    assert folded.element_type == ElementType.SLIDE
    assert folded.heading == "Data sources"
    assert folded.metadata["blocks"] == [
        {"kind": "text", "text": "LHC generates 15 PB a year"},
        {"kind": "table", "text": "| a | b |\n| --- | --- |\n| 1 | 2 |"},
    ]


class _FakeDocling:
    def __init__(self, sections):
        self._sections = sections
        self.range_calls = []

    def parse_pages(self, path, first, last):
        self.range_calls.append((first, last))
        sections = [s for s in self._sections if first <= s.page_number <= last]
        document = Document("fake", path.name, path, DocumentType.PDF, 0, "application/pdf")
        return Ok(ParsedDocument(document, sections, last, metadata={"page_report": []}))


@patch("document_chunk.adapters.parsers.adaptive_pdf_parser.DoclingPdfParser")
def test_scanned_slide_stays_one_slide_after_ocr(docling_cls, tmp_path):
    fake = _FakeDocling(
        [
            Section("Data sources", ElementType.HEADING, "Data sources", 1, page_number=3),
            Section("LHC generates 15 PB a year", ElementType.PARAGRAPH, page_number=3),
        ]
    )
    docling_cls.return_value = fake

    parsed = AdaptivePdfParser(ParserConfig(pdf_slide_mode="auto")).parse(
        _deck(tmp_path, scan_page=True)
    ).unwrap()

    assert fake.range_calls == [(3, 3)]
    page3 = [s for s in parsed.sections if s.page_number == 3]
    assert len(page3) == 1
    assert page3[0].element_type == ElementType.SLIDE
    assert page3[0].heading == "Data sources"
    assert page3[0].content == "LHC generates 15 PB a year"


def _slides_document(sections):
    document = Document("doc-1", "deck.pdf", None, DocumentType.PDF, 0, "application/pdf")
    return ParsedDocument(document, sections, len(sections), metadata={"layout": "slides"})


def test_heading_chunker_keeps_one_chunk_per_pdf_slide():
    doc = _slides_document(
        [
            slide_section(1, "Velocity", [{"kind": "list", "text": "• Data is generated fast"}], source={}),
            slide_section(2, None, [{"kind": "text", "text": "Online data analytics"}], source={}),
            slide_section(3, "Veracity", [], source={}),
        ]
    )

    chunks = HeadingChunker(ChunkerConfig()).chunk(doc).unwrap()

    # Không gộp slide nhỏ vào nhau; slide không tiêu đề nối theo slide trước.
    assert [(c.metadata.page_number, c.metadata.heading_path) for c in chunks] == [
        (1, ("Velocity",)),
        (2, ("Velocity",)),
        (3, ("Veracity",)),
    ]
    assert chunks[2].content == "Veracity"


def test_big_step_numbers_are_not_the_title(tmp_path):
    path = tmp_path / "steps.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=WIDTH, height=HEIGHT)
        page.insert_text((60, 70), "Finding similar documents", fontsize=36)
        for i, step in enumerate(["1", "2", "3"]):
            page.insert_text((180 + i * 280, 200), step, fontsize=48)
            page.insert_text((80 + i * 280, 300), "Shingling converts a document into a set", fontsize=22)
        pdf.save(path)

    assert _parse(path).sections[0].heading == "Finding similar documents"


class _FakeFullDocling:
    def __init__(self, sections):
        self._sections = sections
        self.full_calls = 0

    def parse(self, path):
        self.full_calls += 1
        document = Document("fake", path.name, path, DocumentType.PDF, 0, "application/pdf")
        return Ok(ParsedDocument(document, self._sections, 3, metadata={"page_report": []}))


@patch("document_chunk.adapters.parsers.adaptive_pdf_parser.DoclingPdfParser")
def test_mostly_scanned_deck_is_folded_back_into_slides(docling_cls, tmp_path):
    def scanned(page):
        pixmap = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 480, 270), False)
        pixmap.clear_with(210)
        page.insert_image(fitz.Rect(0, 120, WIDTH, HEIGHT), pixmap=pixmap)

    path = tmp_path / "scanned.pdf"
    with fitz.open() as pdf:
        _slide(pdf, 1, ["Introduction"], ["• Big data is described by four properties of data"])
        _slide(pdf, 2, ["Data sources"], extra=scanned)
        _slide(pdf, 3, ["Volume"], extra=scanned)
        pdf.save(path)
    fake = _FakeFullDocling(
        [
            Section("Introduction", ElementType.HEADING, "Introduction", 1, page_number=1),
            Section("Big data is described by four properties", ElementType.PARAGRAPH, page_number=1),
            Section("LHC generates 15 PB a year", ElementType.PARAGRAPH, page_number=2),
            Section("175 ZB by 2025", ElementType.PARAGRAPH, page_number=3),
        ]
    )
    docling_cls.return_value = fake

    parsed = AdaptivePdfParser(ParserConfig(pdf_slide_mode="auto")).parse(path).unwrap()

    assert fake.full_calls == 1
    assert parsed.metadata["layout"] == "slides"
    assert [(s.page_number, s.heading, s.content) for s in parsed.sections] == [
        (1, "Introduction", "Big data is described by four properties"),
        (2, "Data sources", "LHC generates 15 PB a year"),
        (3, "Volume", "175 ZB by 2025"),
    ]

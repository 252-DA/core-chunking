"""
PdfParser — parse PDF → ParsedDocument dùng PyMuPDF.

Strategy:
  - Extract text blocks với font metadata (size, bold, italic)
  - Detect headings dựa trên font size tương đối so với body text
  - Extract images dạng bytes
  - Fallback: nếu text rỗng (scanned PDF) → raise lỗi rõ ràng
    (OCR sẽ được handle bởi IPreprocessor trước khi vào parser)
"""
import io
import statistics
import uuid
from pathlib import Path

import fitz  # PyMuPDF

from src.domain.entities.document import (
    Document,
    DocumentType,
    ElementType,
    ParsedDocument,
    Section,
)
from src.domain.exceptions import ParseError
from src.domain.ports.parser import IParser
from src.infrastructure.config import ParserConfig
from src.shared.logger import get_logger
from src.shared.result import Err, Ok, Result
from src.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

# Font size nhỏ hơn ngưỡng này coi là caption/footer → bỏ qua
_MIN_FONT_SIZE = 6.0
# Block có ít hơn N ký tự → bỏ qua (header/footer noise)
_MIN_BLOCK_CHARS = 10


class PdfParser(IParser):
    def __init__(self, config: ParserConfig) -> None:
        self._config = config

    @property
    def supported_types(self) -> tuple[DocumentType, ...]:
        return (DocumentType.PDF,)

    def parse(self, path: Path) -> Result[ParsedDocument, Exception]:
        logger.info("pdf_parser.started", file=path.name)
        with tracer.start_as_current_span("pdf_parser.parse") as span:
            span.set_attribute("file.name", path.name)
            span.set_attribute("file.size_bytes", path.stat().st_size)
            try:
                doc = fitz.open(str(path))
                result = self._extract(path, doc)
                doc.close()
                span.set_attribute("pages", result.page_count)
                span.set_attribute("sections", len(result.sections))
                logger.info(
                    "pdf_parser.completed",
                    file=path.name,
                    pages=result.page_count,
                    sections=len(result.sections),
                )
                return Ok(result)

            except Exception as e:
                logger.error("pdf_parser.failed", file=path.name, error=str(e))
                return Err(ParseError(f"Failed to parse PDF '{path.name}'", cause=e))

    # ------------------------------------------------------------------
    # Core extraction
    # ------------------------------------------------------------------

    def _extract(self, path: Path, doc: fitz.Document) -> ParsedDocument:
        max_pages = self._config.pdf_max_pages or len(doc)
        all_spans: list[dict] = []
        images: dict[str, bytes] = {}

        # Pass 1: thu thập tất cả spans để tính font size thống kê
        for page_num in range(min(max_pages, len(doc))):
            page = doc[page_num]
            for span in self._iter_spans(page):
                span["page_number"] = page_num + 1
                all_spans.append(span)

            # Extract images
            for img in page.get_images(full=True):
                xref = img[0]
                try:
                    img_data = doc.extract_image(xref)
                    filename = f"img_{page_num + 1}_{xref}.{img_data['ext']}"
                    images[filename] = img_data["image"]
                except Exception:
                    pass

        if not all_spans:
            raise ValueError(
                f"No text extracted from {path.name}. "
                "File may be scanned — apply OCR preprocessor first."
            )

        # Pass 2: tính heading thresholds từ font size distribution
        heading_thresholds = self._compute_heading_thresholds(all_spans)

        # Pass 3: build sections
        sections = self._build_sections(all_spans, heading_thresholds)

        # Build document entity
        document = Document(
            id=str(uuid.uuid4()),
            name=path.name,
            path=path,
            doc_type=DocumentType.PDF,
            size_bytes=path.stat().st_size,
            mime_type="application/pdf",
        )

        return ParsedDocument(
            document=document,
            sections=sections,
            page_count=min(max_pages, len(doc)),
            images=images,
            language=None,  # language detection là optional, để sau
        )

    # ------------------------------------------------------------------
    # Span extraction
    # ------------------------------------------------------------------

    def _iter_spans(self, page: fitz.Page):
        """Yield text spans từ một page với font metadata."""
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        for block in blocks:
            if block.get("type") != 0:  # 0 = text block
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if not text or len(text) < _MIN_BLOCK_CHARS:
                        continue
                    size = span.get("size", 12.0)
                    if size < _MIN_FONT_SIZE:
                        continue
                    flags = span.get("flags", 0)
                    yield {
                        "text": text,
                        "size": size,
                        "bold": bool(flags & 2**4),   # bit 4 = bold
                        "italic": bool(flags & 2**1), # bit 1 = italic
                        "bbox": span.get("bbox", (0, 0, 0, 0)),
                    }

    # ------------------------------------------------------------------
    # Heading detection
    # ------------------------------------------------------------------

    def _compute_heading_thresholds(self, spans: list[dict]) -> dict:
        """
        Tính ngưỡng font size cho H1, H2, H3 dựa trên distribution.
        Body text = median font size.
        Heading = lớn hơn body text một khoảng nhất định.
        """
        sizes = [s["size"] for s in spans]
        body_size = statistics.median(sizes)
        max_size = max(sizes)

        # Chia khoảng từ body → max thành 3 level
        gap = (max_size - body_size) / 3 if max_size > body_size else 2.0

        return {
            "body": body_size,
            "h3": body_size + gap * 0.8,
            "h2": body_size + gap * 1.5,
            "h1": body_size + gap * 2.2,
        }

    def _detect_heading_level(self, span: dict, thresholds: dict) -> int:
        """Trả về heading level (1-3) hoặc 0 nếu là body text."""
        size = span["size"]
        bold = span["bold"]

        if size >= thresholds["h1"]:
            return 1
        if size >= thresholds["h2"]:
            return 2
        if size >= thresholds["h3"] or (bold and size > thresholds["body"]):
            return 3
        return 0

    # ------------------------------------------------------------------
    # Section building
    # ------------------------------------------------------------------

    def _build_sections(
        self, spans: list[dict], thresholds: dict
    ) -> list[Section]:
        """
        Group spans thành sections.
        Mỗi heading → section mới. Spans giữa 2 headings → 1 section paragraph.
        """
        sections: list[Section] = []
        current_texts: list[str] = []
        current_page: int | None = None

        def flush_paragraph():
            content = " ".join(current_texts).strip()
            if content:
                sections.append(Section(
                    content=content,
                    element_type=ElementType.PARAGRAPH,
                    page_number=current_page,
                ))
            current_texts.clear()

        for span in spans:
            level = self._detect_heading_level(span, thresholds)
            page = span.get("page_number")

            if level > 0:
                # Flush paragraph buffer trước heading
                flush_paragraph()
                sections.append(Section(
                    content=span["text"],
                    element_type=ElementType.HEADING,
                    heading=span["text"],
                    heading_level=level,
                    page_number=page,
                ))
                current_page = page
            else:
                current_texts.append(span["text"])
                current_page = page

        # Flush phần còn lại
        flush_paragraph()

        return sections

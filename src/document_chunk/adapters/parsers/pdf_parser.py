"""
PdfParser — PyMuPDF text path, đánh giá độ đầy đủ theo TỪNG TRANG.

Parser này không tự kết luận "file đọc được" hay "file không đọc được". Nó đo
từng trang rồi gắn status (xem ``pdf_page_assessment``):

  - ``text``           → text layer dùng được, giữ kết quả
  - ``complex_layout`` → có text nhưng nhiều cột / có bảng: thứ tự đọc và quan
                         hệ hàng–cột không đảm bảo → cần layout backend
  - ``needs_ocr``      → không có text layer dùng được (trang scan, trang vector)
  - ``empty``          → trang trắng thật, không phải lỗi

Kết quả đi kèm ``metadata['page_report']`` (một dòng mỗi trang, có số trang và
bbox nguồn để đối chiếu) và ``metadata['coverage']`` (tổng hợp phần chưa đọc
được). ``AdaptivePdfParser`` đọc report này để OCR đúng những trang thiếu.

Ngân sách xử lý áp cho từng tài liệu: dung lượng file, số trang, thời gian, và
dung lượng bytes ảnh. ``pdf_extract_images`` mặc định tắt vì chưa có consumer
cho bytes ảnh.

Giới hạn đã biết: bảng chỉ được phát hiện qua đường kẻ (PyMuPDF
``find_tables``), bảng dàn bằng khoảng trắng sẽ không bị gắn cờ.
"""
import math
import re
import statistics
import time
import uuid
from collections import defaultdict
from pathlib import Path

import fitz  # PyMuPDF

from document_chunk.adapters.parsers.pdf_page_assessment import (
    PageAssessment,
    PageStatus,
    build_report,
    classify_page,
    coverage,
    has_column_gutter,
    missing_text_error,
)
from document_chunk.domain.entities.document import (
    Document,
    DocumentType,
    ElementType,
    ParsedDocument,
    Section,
)
from document_chunk.domain.exceptions import ParseBudgetExceededError, ParseError
from document_chunk.domain.ports.parser import IParser
from document_chunk.infrastructure.config import ParserConfig
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

PARSER_NAME = "pymupdf"

#: Dải trên/dưới trang coi là lề — nơi header/footer chạy lặp.
_MARGIN_BAND = 0.07


class _Budget:
    """
    Ngân sách xử lý cho MỘT tài liệu.

    ``concurrency=1`` chỉ giới hạn số tài liệu xử lý song song; một file 800
    trang vẫn có thể chiếm worker vô hạn định. Ngân sách này chặn theo dung
    lượng, thời gian và bytes ảnh tích lũy.
    """

    def __init__(self, config: ParserConfig, size_bytes: int) -> None:
        self._max_file_bytes = config.pdf_max_file_bytes
        self._timeout = config.pdf_timeout_seconds
        self._max_image_bytes = config.pdf_max_image_bytes
        self._size_bytes = size_bytes
        self._image_bytes = 0
        self._started = time.monotonic()
        self.image_budget_exhausted = False

    def check_file_size(self, file_name: str) -> None:
        if self._max_file_bytes is not None and self._size_bytes > self._max_file_bytes:
            raise ParseBudgetExceededError(
                f"'{file_name}' nặng {self._size_bytes} bytes, vượt hạn mức "
                f"{self._max_file_bytes} bytes",
                limit="pdf_max_file_bytes",
                observed=str(self._size_bytes),
            )

    def check_deadline(self, *, file_name: str, page: int) -> None:
        if self._timeout is None:
            return
        elapsed = time.monotonic() - self._started
        if elapsed > self._timeout:
            raise ParseBudgetExceededError(
                f"'{file_name}' vượt hạn mức {self._timeout}s khi đang xử lý "
                f"trang {page} (đã dùng {elapsed:.1f}s)",
                limit="pdf_timeout_seconds",
                observed=f"{elapsed:.1f}s",
            )

    def take_image_bytes(self, count: int) -> bool:
        """False khi ngân sách ảnh đã hết — parser dừng trích ảnh, không dừng parse."""
        if self._image_bytes + count > self._max_image_bytes:
            self.image_budget_exhausted = True
            return False
        self._image_bytes += count
        return True

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    @property
    def image_bytes(self) -> int:
        return self._image_bytes


class PdfParser(IParser):
    """
    PyMuPDF text path.

    ``enforce_coverage=True`` (mặc định) → trả Err khi còn trang chưa đọc được,
    để parser dùng độc lập không âm thầm bỏ trang. ``AdaptivePdfParser`` tạo
    parser này với ``enforce_coverage=False`` vì nó áp chính sách sau khi đã
    chạy fallback.
    """

    def __init__(self, config: ParserConfig, *, enforce_coverage: bool = True) -> None:
        self._config = config
        self._enforce_coverage = enforce_coverage

    @property
    def supported_types(self) -> tuple[DocumentType, ...]:
        return (DocumentType.PDF,)

    def parse(self, path: Path) -> Result[ParsedDocument, Exception]:
        logger.info("pdf_parser.started", file=path.name)
        with tracer.start_as_current_span("pdf_parser.parse") as span:
            span.set_attribute("file.name", path.name)
            try:
                size_bytes = path.stat().st_size
                span.set_attribute("file.size_bytes", size_bytes)

                budget = _Budget(self._config, size_bytes)
                budget.check_file_size(path.name)

                with fitz.open(str(path)) as doc:
                    result = self._extract(path, doc, budget, size_bytes)

            except ParseBudgetExceededError as exc:
                logger.error(
                    "pdf_parser.budget_exceeded",
                    file=path.name,
                    limit=exc.limit,
                    observed=exc.observed,
                )
                return Err(exc)
            except Exception as exc:
                logger.error("pdf_parser.failed", file=path.name, error=str(exc))
                return Err(ParseError(f"Failed to parse PDF '{path.name}'", cause=exc))

            cov = result.metadata["coverage"]
            span.set_attribute("pages", result.page_count)
            span.set_attribute("sections", len(result.sections))
            span.set_attribute("coverage.status", cov["status"])
            span.set_attribute("coverage.unprocessed_pages", len(cov["unprocessed_pages"]))
            logger.info(
                "pdf_parser.completed",
                file=path.name,
                pages=result.page_count,
                sections=len(result.sections),
                coverage=cov["status"],
                by_status=cov["by_status"],
                elapsed_seconds=round(budget.elapsed_seconds, 2),
            )

            if self._enforce_coverage:
                error = missing_text_error(cov, file_name=path.name, parsed=result)
                if error is not None:
                    logger.error(
                        "pdf_parser.incomplete_coverage",
                        file=path.name,
                        pages=error.pages,
                    )
                    return Err(error)

            return Ok(result)

    # ------------------------------------------------------------------
    # Core extraction
    # ------------------------------------------------------------------

    def _extract(
        self,
        path: Path,
        doc: fitz.Document,
        budget: _Budget,
        size_bytes: int,
    ) -> ParsedDocument:
        pages_total = doc.page_count
        if pages_total == 0:
            raise ValueError(f"'{path.name}' không có trang nào")

        limit = self._config.pdf_max_pages
        pages_examined = min(limit, pages_total) if limit else pages_total

        # Pass 1: lấy dòng text theo từng trang, giữ nguyên vị trí nguồn.
        pages: dict[int, dict] = {}
        for index in range(pages_examined):
            page_number = index + 1
            budget.check_deadline(file_name=path.name, page=page_number)
            page = doc[index]
            pages[page_number] = {
                "width": page.rect.width,
                "height": page.rect.height,
                "lines": list(self._iter_lines(page)),
            }

        # Pass 2: bỏ header/footer chạy lặp trước khi đo — nếu không, trang scan
        # có số trang stamp sẵn sẽ bị tính là "có text".
        self._drop_running_marginals(pages)

        # Pass 3: đo và phân loại từng trang (+ trích ảnh nếu được bật).
        images: dict[str, bytes] = {}
        assessments: list[PageAssessment] = []
        for page_number in range(1, pages_examined + 1):
            budget.check_deadline(file_name=path.name, page=page_number)
            page = doc[page_number - 1]
            assessments.append(self._assess_page(page, pages[page_number], page_number))
            if self._config.pdf_extract_images:
                self._collect_images(doc, page, page_number, images, budget)

        # Pass 4: heading thresholds + sections, theo thứ tự trang.
        body_lines = [
            line
            for page_number in sorted(pages)
            for line in pages[page_number]["lines"]
        ]
        sections = (
            self._build_sections(pages, self._compute_heading_thresholds(body_lines))
            if body_lines
            else []
        )

        needs_fallback = [a.page_number for a in assessments if a.needs_fallback]
        if not sections and not images and not needs_fallback:
            raise ValueError(
                f"Không lấy được nội dung nào từ '{path.name}' và không trang nào "
                "cần OCR — file có thể trống hoặc hỏng."
            )

        report = build_report(assessments, backend=PARSER_NAME)
        cov = coverage(report, pages_total=pages_total, pages_examined=pages_examined)
        if needs_fallback:
            logger.warning(
                "pdf_parser.pages_need_fallback",
                file=path.name,
                pages=len(needs_fallback),
                needs_ocr=cov["by_status"].get(PageStatus.NEEDS_OCR.value, 0),
                complex_layout=cov["by_status"].get(PageStatus.COMPLEX_LAYOUT.value, 0),
            )

        document = Document(
            id=str(uuid.uuid4()),
            name=path.name,
            path=path,
            doc_type=DocumentType.PDF,
            size_bytes=size_bytes,
            mime_type="application/pdf",
        )

        return ParsedDocument(
            document=document,
            sections=sections,
            page_count=pages_examined,
            images=images,
            language=None,
            metadata={
                "parser": PARSER_NAME,
                "page_report": report,
                "coverage": cov,
                "budget": {
                    "max_file_bytes": self._config.pdf_max_file_bytes,
                    "timeout_seconds": self._config.pdf_timeout_seconds,
                    "extract_images": self._config.pdf_extract_images,
                    "image_bytes": budget.image_bytes,
                    "images_truncated": budget.image_budget_exhausted,
                    "elapsed_seconds": round(budget.elapsed_seconds, 3),
                },
            },
        )

    # ------------------------------------------------------------------
    # Span extraction
    # ------------------------------------------------------------------

    def _iter_lines(self, page: fitz.Page):
        """Keep short styled spans and paragraph boundaries from the source."""
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        for block_index, block in enumerate(blocks):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if s.get("text")]
                text = "".join(s["text"] for s in spans).strip()
                if not text:
                    continue
                dominant = max(spans, key=lambda s: len(s["text"].strip()))
                bbox = line.get("bbox", (0, 0, 0, 0))
                yield {
                    "text": text, "size": dominant.get("size", 12),
                    "bold": bool(dominant.get("flags", 0) & 16),
                    "italic": bool(dominant.get("flags", 0) & 2),
                    "bbox": bbox, "block_index": block_index,
                    "margin": (
                        bbox[3] <= page.rect.height * _MARGIN_BAND
                        or bbox[1] >= page.rect.height * (1 - _MARGIN_BAND)
                    ),
                }

    def _drop_running_marginals(self, pages: dict[int, dict]) -> None:
        """Only remove repeated marginal lines across at least three pages."""
        occurrences: dict[str, set[int]] = defaultdict(set)
        for page_number, page in pages.items():
            for line in page["lines"]:
                if line["margin"]:
                    occurrences[_marginal_key(line["text"])].add(page_number)

        threshold = max(3, math.ceil(len(pages) * .3))
        for page in pages.values():
            page["lines"] = [
                line for line in page["lines"]
                if not (
                    line["margin"]
                    and len(occurrences[_marginal_key(line["text"])]) >= threshold
                )
            ]

    # ------------------------------------------------------------------
    # Per-page assessment
    # ------------------------------------------------------------------

    def _assess_page(
        self, page: fitz.Page, page_text: dict, page_number: int
    ) -> PageAssessment:
        lines = [line for line in page_text["lines"] if not line["margin"]]
        char_count = sum(len(line["text"]) for line in lines)
        min_chars = self._config.pdf_min_page_chars
        image_area_ratio = self._image_area_ratio(page)

        # Vector-only page: không ảnh, không text layer → vẫn phải OCR ảnh render.
        # get_drawings() không rẻ nên chỉ gọi khi trang thực sự thiếu text.
        drawing_count = 0
        if char_count < min_chars and image_area_ratio < self._config.pdf_scan_image_coverage:
            try:
                drawing_count = len(page.get_drawings())
            except Exception as exc:  # pragma: no cover - PyMuPDF backend specific
                logger.debug(
                    "pdf_parser.drawings_failed", page=page_number, error=str(exc)
                )

        multi_column = False
        table_count = 0
        if char_count >= min_chars:
            multi_column = has_column_gutter(
                [line["bbox"] for line in lines], page_text["width"]
            )
            table_count = self._table_count(page, page_number)

        return classify_page(
            page_number=page_number,
            char_count=char_count,
            line_count=len(lines),
            image_area_ratio=image_area_ratio,
            drawing_count=drawing_count,
            multi_column=multi_column,
            table_count=table_count,
            min_page_chars=min_chars,
            scan_image_coverage=self._config.pdf_scan_image_coverage,
        )

    def _image_area_ratio(self, page: fitz.Page) -> float:
        page_area = abs(page.rect.get_area())
        if page_area <= 0:
            return 0.0
        covered = 0.0
        try:
            infos = page.get_image_info()
        except Exception as exc:  # pragma: no cover - PyMuPDF backend specific
            logger.debug("pdf_parser.image_info_failed", error=str(exc))
            return 0.0
        for info in infos:
            clipped = fitz.Rect(info["bbox"]) & page.rect
            if not clipped.is_empty:
                covered += clipped.get_area()
        return min(covered / page_area, 1.0)

    def _table_count(self, page: fitz.Page, page_number: int) -> int:
        if not self._config.pdf_detect_tables:
            return 0
        try:
            return len(page.find_tables().tables)
        except Exception as exc:  # pragma: no cover - PyMuPDF backend specific
            logger.debug("pdf_parser.find_tables_failed", page=page_number, error=str(exc))
            return 0

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------

    def _collect_images(
        self,
        doc: fitz.Document,
        page: fitz.Page,
        page_number: int,
        images: dict[str, bytes],
        budget: _Budget,
    ) -> None:
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                img_data = doc.extract_image(xref)
            except Exception:
                continue
            payload = img_data["image"]
            if not budget.take_image_bytes(len(payload)):
                logger.warning(
                    "pdf_parser.image_budget_reached",
                    page=page_number,
                    collected=len(images),
                    limit_bytes=self._config.pdf_max_image_bytes,
                )
                return
            images[f"img_{page_number}_{xref}.{img_data['ext']}"] = payload

    # ------------------------------------------------------------------
    # Heading detection
    # ------------------------------------------------------------------

    def _compute_heading_thresholds(self, spans: list[dict]) -> dict:
        """
        Tính ngưỡng font size cho H1, H2, H3 dựa trên distribution.
        Body text = median font size.
        Heading = lớn hơn body text một khoảng nhất định.

        Đây vẫn là heuristic: cỡ chữ lớn không chắc là heading. Trang nào layout
        phức tạp đã được gắn cờ ở bước đánh giá để layout backend xử lý.
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

    def _build_sections(self, pages: dict[int, dict], thresholds: dict) -> list[Section]:
        sections: list[Section] = []
        for page_number in sorted(pages):
            sections.extend(
                self._page_sections(page_number, pages[page_number]["lines"], thresholds)
            )
        return sections

    def _page_sections(
        self, page_number: int, lines: list[dict], thresholds: dict
    ) -> list[Section]:
        """
        Group lines của một trang thành sections.

        Mỗi heading → section mới; các dòng trong cùng một text block → một
        paragraph. Mỗi section giữ vị trí nguồn (trang + bbox + block) để đối
        chiếu lại với PDF khi kiểm tra kết quả.
        """
        sections: list[Section] = []
        buffer: list[dict] = []

        def flush_paragraph() -> None:
            if not buffer:
                return
            content = " ".join(line["text"] for line in buffer).strip()
            if content:
                sections.append(Section(
                    content=content,
                    element_type=ElementType.PARAGRAPH,
                    page_number=page_number,
                    metadata={"source": _source(page_number, buffer)},
                ))
            buffer.clear()

        previous_block = None
        for line in lines:
            if line["block_index"] != previous_block:
                flush_paragraph()
            previous_block = line["block_index"]

            if self._detect_heading_level(line, thresholds) > 0:
                flush_paragraph()
                level = self._detect_heading_level(line, thresholds)
                sections.append(Section(
                    content=line["text"],
                    element_type=ElementType.HEADING,
                    heading=line["text"],
                    heading_level=level,
                    page_number=page_number,
                    metadata={"source": _source(page_number, [line])},
                ))
            else:
                buffer.append(line)

        flush_paragraph()
        return sections


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _marginal_key(text: str) -> str:
    """Chuẩn hóa dòng lề để so khớp header/footer chạy lặp giữa các trang."""
    return re.sub(r"\d+", "#", text.casefold())


def _source(page_number: int, lines: list[dict]) -> dict:
    """Vị trí nguồn của một section — dùng để kiểm tra lại kết quả trên PDF."""
    boxes = [line["bbox"] for line in lines]
    return {
        "parser": PARSER_NAME,
        "page": page_number,
        "block": lines[0]["block_index"],
        "lines": len(lines),
        "bbox": [
            round(min(b[0] for b in boxes), 1),
            round(min(b[1] for b in boxes), 1),
            round(max(b[2] for b in boxes), 1),
            round(max(b[3] for b in boxes), 1),
        ],
    }

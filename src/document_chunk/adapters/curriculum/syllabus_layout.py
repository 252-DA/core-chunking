"""
SyllabusLayout — cách nhìn PDF đề cương theo **trang và bảng**, không làm phẳng.

Lý do tồn tại: bảng mục 6 của DCMH đặt chương ở cột giữa và LO/AO ở cột phải,
và một hàng có thể kéo dài qua hai trang. Nếu nối toàn bộ chữ thành một chuỗi
thì LO của hàng sau dễ bị gắn vào chương của hàng trước, và ba chữ số khác
nghĩa (số buổi, số chương, số mục trong giáo trình) trộn lẫn không phân biệt
được. Reader này giữ nguyên ô của bảng và số trang để mọi dữ kiện trích ra đều
chỉ được về nguồn.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

try:  # PyMuPDF ≥ 1.24 đổi tên gói; giữ fallback cho bản cũ.
    import pymupdf
except ImportError:  # pragma: no cover - phụ thuộc phiên bản môi trường
    import fitz as pymupdf

from document_chunk.domain.exceptions import ProcessingError
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)

# Tỉ lệ chiều cao trang được coi là lề trên/dưới khi tìm header/footer chạy lặp.
_MARGIN_BAND = 0.12
_MARGINAL_MIN_PAGES = 3
_MARGINAL_PAGE_RATIO = 0.3

_RE_WS = re.compile(r"\s+")
_RE_DIGITS = re.compile(r"\d+")


def _marginal_key(text: str) -> str:
    """Chuẩn hoá dòng lề: bỏ chữ số để 'about:blank 3/14' và '4/14' trùng nhau."""
    return _RE_DIGITS.sub("#", _RE_WS.sub(" ", text).strip().lower())


@dataclass(frozen=True)
class LayoutTable:
    page: int                                   # 1-based
    index: int                                  # thứ tự bảng trong trang
    rows: tuple[tuple[str, ...], ...]

    def cell(self, row: int, col: int) -> str:
        try:
            return self.rows[row][col] or ""
        except IndexError:
            return ""

    def locator(self, row: int | None = None, col: int | None = None) -> str:
        parts = [f"bảng {self.index}"]
        if row is not None:
            parts.append(f"hàng {row}")
        if col is not None:
            parts.append(f"cột {col}")
        return " ".join(parts)


@dataclass(frozen=True)
class LayoutPage:
    number: int                                 # 1-based
    text: str                                   # đã bỏ header/footer chạy lặp
    tables: tuple[LayoutTable, ...] = ()


class SyllabusLayout:
    """Text nối liền qua các trang, kèm chỉ mục offset → số trang, và các bảng."""

    def __init__(self, pages: tuple[LayoutPage, ...]) -> None:
        self.pages = pages
        chunks: list[str] = []
        spans: list[tuple[int, int, int]] = []   # (start, end, page_number)
        offset = 0
        for page in pages:
            body = page.text
            chunks.append(body)
            spans.append((offset, offset + len(body), page.number))
            offset += len(body) + 1              # +1 cho "\n" nối trang
        self.text = "\n".join(chunks)
        self._spans = tuple(spans)

    @property
    def tables(self) -> tuple[LayoutTable, ...]:
        return tuple(t for page in self.pages for t in page.tables)

    def page_at(self, offset: int) -> int | None:
        """Số trang chứa ký tự ở vị trí ``offset`` trong ``self.text``."""
        for start, end, page_number in self._spans:
            if start <= offset <= end:
                return page_number
        return None


class PyMuPdfSyllabusReader:
    """Đọc PDF đề cương thành SyllabusLayout. Chỉ dùng text layer, không OCR."""

    def read(self, path: Path) -> Result[SyllabusLayout, Exception]:
        try:
            with pymupdf.open(str(path)) as doc:
                raw_pages = [self._read_page(doc[i], i + 1) for i in range(doc.page_count)]
        except Exception as exc:
            return Err(ProcessingError(f"Cannot read syllabus PDF: {path}", cause=exc))

        self._drop_running_marginals(raw_pages)

        pages = tuple(
            LayoutPage(
                number=raw["number"],
                text="\n".join(line["text"] for line in raw["lines"]),
                tables=tuple(raw["tables"]),
            )
            for raw in raw_pages
        )
        layout = SyllabusLayout(pages)
        logger.info(
            "syllabus_layout.read",
            file=path.name,
            pages=len(pages),
            tables=len(layout.tables),
            chars=len(layout.text),
        )
        return Ok(layout)

    # ------------------------------------------------------------------

    def _read_page(self, page, number: int) -> dict:
        height = float(page.rect.height) or 1.0
        top_band = height * _MARGIN_BAND
        bottom_band = height * (1.0 - _MARGIN_BAND)

        lines: list[dict] = []
        raw = page.get_text("dict")
        for block in raw.get("blocks", ()):
            for line in block.get("lines", ()):
                text = "".join(span.get("text", "") for span in line.get("spans", ()))
                if not text.strip():
                    continue
                y0 = line.get("bbox", (0, 0, 0, 0))[1]
                lines.append({
                    "text": text.strip(),
                    "margin": y0 < top_band or y0 > bottom_band,
                })

        tables: list[LayoutTable] = []
        try:
            found = page.find_tables()
        except Exception as exc:  # pragma: no cover - phụ thuộc backend PyMuPDF
            logger.debug("syllabus_layout.find_tables_failed", page=number, error=str(exc))
            found = None
        if found is not None:
            for ti, table in enumerate(found.tables):
                try:
                    extracted = table.extract()
                except Exception as exc:  # pragma: no cover
                    logger.debug(
                        "syllabus_layout.table_extract_failed",
                        page=number, table=ti, error=str(exc),
                    )
                    continue
                rows = tuple(
                    tuple((cell or "").strip() for cell in row)
                    for row in extracted
                )
                tables.append(LayoutTable(page=number, index=ti, rows=rows))

        return {"number": number, "lines": lines, "tables": tables}

    def _drop_running_marginals(self, pages: list[dict]) -> None:
        """Bỏ dòng lề lặp lại trên nhiều trang (header trường, mã đề cương, footer)."""
        occurrences: dict[str, set[int]] = defaultdict(set)
        for page in pages:
            for line in page["lines"]:
                if line["margin"]:
                    occurrences[_marginal_key(line["text"])].add(page["number"])

        threshold = max(_MARGINAL_MIN_PAGES, math.ceil(len(pages) * _MARGINAL_PAGE_RATIO))
        for page in pages:
            page["lines"] = [
                line for line in page["lines"]
                if not (
                    line["margin"]
                    and len(occurrences[_marginal_key(line["text"])]) >= threshold
                )
            ]

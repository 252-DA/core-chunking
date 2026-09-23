"""
AdaptivePdfParser — route TỪNG TRANG tới backend phù hợp, rồi ghép theo số trang.

Trước đây việc chọn backend dựa trên một câu hỏi duy nhất: "PyMuPDF có đọc được
text không?". Một giáo trình 100 trang có 90 trang text và 10 trang scan vẫn
được tính là thành công, và 10 trang scan không bao giờ được OCR.

Luồng hiện tại:

    Kiểm tra file và ngân sách xử lý
                  ↓
    PyMuPDF đọc và đánh giá từng trang
                  ↓
    Trang text đơn giản → giữ kết quả
    Trang thiếu text / layout phức tạp → Docling (OCR + layout) theo khoảng trang
                  ↓
    Ghép theo thứ tự trang, giữ heading / paragraph / table
                  ↓
    Kiểm tra độ đầy đủ → Ok, hoặc Err nêu rõ trang chưa đọc được

Ba kết cục được phân biệt rõ trong ``metadata['coverage']``:
  - ``unprocessed_pages`` — chưa backend nào đọc → mất dữ liệu, Err (mặc định)
  - ``no_text_pages``     — đã OCR nhưng không có text (ảnh không chữ) → ghi nhận
  - ``degraded_pages``    — có text, layout chưa qua layout backend → cảnh báo
"""

import gc
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from document_chunk.adapters.parsers import pdf_page_assessment as assess
from document_chunk.adapters.parsers.docling_pdf_parser import (
    PARSER_NAME as DOCLING_BACKEND,
)
from document_chunk.adapters.parsers.docling_pdf_parser import DoclingPdfParser
from document_chunk.adapters.parsers.pdf_parser import PARSER_NAME as TEXT_BACKEND
from document_chunk.adapters.parsers.pdf_parser import PdfParser
from document_chunk.domain.entities.document import (
    DocumentType,
    ParsedDocument,
    Section,
)
from document_chunk.infrastructure.config import ParserConfig
from document_chunk.domain.ports.parser import IParser
from document_chunk.shared.logger import get_logger
from document_chunk.shared.metrics import PDF_PAGES
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)


class AdaptivePdfParser(IParser):
    """PyMuPDF cho trang text; Docling cho đúng những trang cần OCR/layout."""

    def __init__(self, config: ParserConfig) -> None:
        self._config = config
        # Chính sách độ đầy đủ được áp ở đây, sau khi fallback đã chạy — nên text
        # parser không được tự trả Err khi còn trang thiếu.
        self._text_parser = PdfParser(config, enforce_coverage=False)

    @property
    def supported_types(self) -> tuple[DocumentType, ...]:
        return (DocumentType.PDF,)

    def parse(self, path: Path) -> Result[ParsedDocument, Exception]:
        text_result = self._text_parser.parse(path)
        if text_result.is_err():
            # Không mở/đọc được bằng text path (file mã hóa, hỏng header, vượt
            # ngân sách…) → không có đánh giá theo trang để route, đưa cả file
            # cho Docling.
            return self._full_fallback(path, reason=str(text_result.error))

        parsed = text_result.unwrap()
        report = parsed.metadata.get("page_report")
        if not report:
            # Parser không báo cáo theo trang → không có gì để route.
            return Ok(parsed)

        pending = assess.pages_needing_fallback(report)
        if not pending:
            logger.info(
                "adaptive_pdf_parser.selected",
                file=path.name,
                backend=TEXT_BACKEND,
                pages=parsed.page_count,
            )
            return self._finalize(path, parsed)

        if not self._config.pdf_page_fallback_enabled:
            logger.warning(
                "adaptive_pdf_parser.fallback_disabled",
                file=path.name,
                pages=assess.format_pages(pending),
            )
            return self._finalize(path, parsed)

        examined = parsed.page_count or len(report)
        ratio = len(pending) / examined if examined else 1.0
        if ratio >= self._config.pdf_full_fallback_page_ratio:
            # Phần lớn tài liệu cần backend layout — chạy một lần cho cả file
            # rẻ hơn và giữ được thứ tự đọc toàn cục.
            return self._full_fallback(
                path,
                reason=f"{len(pending)}/{examined} pages need fallback",
                text_parsed=parsed,
            )

        return self._parse_pending_pages(path, parsed, report, pending)

    # ------------------------------------------------------------------
    # Fallback theo khoảng trang + ghép kết quả
    # ------------------------------------------------------------------

    def _parse_pending_pages(
        self,
        path: Path,
        parsed: ParsedDocument,
        report: list[dict],
        pending: list[int],
    ) -> Result[ParsedDocument, Exception]:
        ranges = assess.contiguous_ranges(pending, self._config.pdf_max_fallback_ranges)
        logger.info(
            "adaptive_pdf_parser.page_fallback",
            file=path.name,
            backend=DOCLING_BACKEND,
            pages=assess.format_pages(pending),
            ranges=[list(r) for r in ranges],
        )

        replacements: dict[int, list[Section]] = {}
        with self._docling() as docling:
            for first, last in ranges:
                pages = range(first, last + 1)
                result = docling.parse_pages(path, first, last)
                if result.is_err():
                    # Backend lỗi → những trang này vẫn chưa ai đọc, không được
                    # tính là "đã OCR nhưng không có chữ".
                    logger.warning(
                        "adaptive_pdf_parser.range_failed",
                        file=path.name,
                        page_range=[first, last],
                        error=str(result.error),
                    )
                    assess.mark_failure(report, pages, DOCLING_BACKEND)
                    continue
                assess.mark_attempt(report, pages, DOCLING_BACKEND)
                replacements.update(
                    self._sections_by_page(path, result.unwrap(), first, last)
                )

        for page in replacements:
            assess.mark_parsed(report, [page], DOCLING_BACKEND, layout_aware=True)

        parsed.sections = self._merge_sections(parsed, replacements)
        return self._finalize(path, parsed)

    def _sections_by_page(
        self, path: Path, ranged: ParsedDocument, first: int, last: int
    ) -> dict[int, list[Section]]:
        """
        Nhóm sections của một lần chạy Docling theo số trang.

        Docling giữ số trang tuyệt đối khi convert theo khoảng. Section không có
        số trang được gán vào trang đầu khoảng; section rơi ra ngoài khoảng bị
        bỏ vì trang đó đã có kết quả từ text path.
        """
        buckets: dict[int, list[Section]] = {}
        outside = 0
        for section in ranged.sections:
            if not section.content.strip():
                # Không thay nội dung PyMuPDF bằng section rỗng của backend khác.
                continue
            page = section.page_number if section.page_number is not None else first
            if not (first <= page <= last):
                outside += 1
                continue
            buckets.setdefault(page, []).append(self._stamp_source(section, page))

        if outside:
            logger.warning(
                "adaptive_pdf_parser.sections_outside_range",
                file=path.name,
                page_range=[first, last],
                dropped=outside,
            )
        return buckets

    def _stamp_source(self, section: Section, page: int) -> Section:
        """Giữ số trang và backend nguồn để đối chiếu lại với PDF."""
        source = {
            "parser": DOCLING_BACKEND,
            "page": page,
            "element": section.metadata.get("docling_type"),
        }
        return replace(
            section,
            page_number=page,
            metadata={**section.metadata, "source": source},
        )

    def _merge_sections(
        self, parsed: ParsedDocument, replacements: dict[int, list[Section]]
    ) -> list[Section]:
        """
        Ghép theo thứ tự trang: trang nào Docling đọc lại thì dùng bản của
        Docling, các trang còn lại giữ bản PyMuPDF.
        """
        if not replacements:
            return parsed.sections

        by_page: dict[int | None, list[Section]] = {}
        for section in parsed.sections:
            by_page.setdefault(section.page_number, []).append(section)

        merged: list[Section] = []
        for page in range(1, parsed.page_count + 1):
            merged.extend(replacements.get(page) or by_page.get(page, []))
        merged.extend(by_page.get(None, []))
        return merged

    # ------------------------------------------------------------------
    # Fallback cả tài liệu
    # ------------------------------------------------------------------

    def _full_fallback(
        self,
        path: Path,
        *,
        reason: str,
        text_parsed: ParsedDocument | None = None,
    ) -> Result[ParsedDocument, Exception]:
        logger.warning(
            "adaptive_pdf_parser.fallback",
            file=path.name,
            backend=DOCLING_BACKEND,
            reason=reason,
        )
        limit = self._config.pdf_max_pages
        with self._docling() as docling:
            # Hạn mức số trang cũng phải áp cho backend đắt: không OCR 800 trang
            # khi ngân sách chỉ cho phép đọc 100 trang đầu.
            result = (
                docling.parse_pages(path, 1, limit) if limit else docling.parse(path)
            )

        if result.is_err():
            if text_parsed is not None:
                # Vẫn còn kết quả text path — báo Ok/Err theo độ đầy đủ thật sự
                # thay vì bỏ luôn những trang đã đọc được.
                logger.error(
                    "adaptive_pdf_parser.fallback_failed",
                    file=path.name,
                    error=str(result.error),
                )
                return self._finalize(path, text_parsed)
            return result

        parsed = result.unwrap()
        if text_parsed is not None:
            self._carry_over_report(parsed, text_parsed)
        return self._finalize(path, parsed)

    def _carry_over_report(
        self, parsed: ParsedDocument, text_parsed: ParsedDocument
    ) -> None:
        """
        Gộp đánh giá của text path vào report của Docling.

        Docling không phân biệt được "trang trắng" và "trang có ảnh nhưng không
        đọc ra chữ". Text path biết, nên trang đã được đánh giá ``empty`` vẫn là
        ``empty`` — không bị báo thành trang chưa đọc được.
        """
        previous = text_parsed.metadata.get("coverage") or {}
        # Docling chạy theo khoảng không biết tổng số trang thật của file.
        parsed.metadata["coverage"] = {
            "pages_total": previous.get("pages_total", parsed.page_count),
            "pages_examined": previous.get("pages_examined", parsed.page_count),
        }

        text_report = {e["page"]: e for e in text_parsed.metadata.get("page_report", [])}
        for entry in parsed.metadata.get("page_report", []):
            source = text_report.get(entry["page"])
            if source is None:
                continue
            entry["attempts"] = [TEXT_BACKEND, *entry.get("attempts", [])]
            if entry.get("parsed_by"):
                continue
            if source["status"] == assess.PageStatus.EMPTY.value:
                entry["status"] = assess.PageStatus.EMPTY.value
                entry["reasons"] = list(source.get("reasons", []))
            else:
                entry["image_area_ratio"] = source.get("image_area_ratio", 0.0)

    @contextmanager
    def _docling(self):
        """
        Docling giữ vài model OCR/layout. Không để chúng nằm trong RAM khi
        BGE model được nạp ở stage tiếp theo của pipeline.
        """
        parser = DoclingPdfParser(self._config)
        try:
            yield parser
        finally:
            del parser
            gc.collect()

    # ------------------------------------------------------------------
    # Độ đầy đủ
    # ------------------------------------------------------------------

    def _finalize(
        self, path: Path, parsed: ParsedDocument
    ) -> Result[ParsedDocument, Exception]:
        report = parsed.metadata.get("page_report") or []
        previous = parsed.metadata.get("coverage") or {}
        pages_total = previous.get("pages_total") or parsed.page_count or len(report)
        pages_examined = previous.get("pages_examined") or parsed.page_count or len(report)
        cov = assess.coverage(
            report, pages_total=pages_total, pages_examined=pages_examined
        )
        parsed.metadata["coverage"] = cov

        for entry in report:
            PDF_PAGES.labels(
                status=entry["status"], backend=entry.get("parsed_by") or "none"
            ).inc()

        if cov["no_text_pages"]:
            logger.warning(
                "adaptive_pdf_parser.pages_without_text",
                file=path.name,
                pages=assess.format_pages(cov["no_text_pages"]),
            )
        if cov["degraded_pages"]:
            logger.warning(
                "adaptive_pdf_parser.pages_layout_degraded",
                file=path.name,
                pages=assess.format_pages(cov["degraded_pages"]),
            )
        if cov["pages_skipped_by_limit"]:
            logger.warning(
                "adaptive_pdf_parser.pages_skipped_by_limit",
                file=path.name,
                ranges=cov["pages_skipped_by_limit"],
                limit=self._config.pdf_max_pages,
            )

        error = assess.missing_text_error(cov, file_name=path.name, parsed=parsed)
        if error is None:
            logger.info(
                "adaptive_pdf_parser.completed",
                file=path.name,
                coverage=cov["status"],
                sections=len(parsed.sections),
                by_backend=cov["by_backend"],
            )
            return Ok(parsed)

        logger.error(
            "adaptive_pdf_parser.incomplete_coverage",
            file=path.name,
            pages=assess.format_pages(error.pages),
            strict=self._config.pdf_strict_missing_text,
        )
        if self._config.pdf_strict_missing_text:
            return Err(error)
        return Ok(parsed)

"""
DoclingPdfParser — parse PDF → ParsedDocument dùng Docling.

Strategy:
  - Sử dụng Docling DocumentConverter để extract text, bảng, và ảnh từ PDF.
  - do_table_structure=True: extract bảng có cấu trúc (markdown output).
  - do_ocr=True + force_full_page_ocr=False: auto-detect OCR theo từng trang
    (dùng text layer khi có, OCR khi text layer thiếu/rỗng).
  - do_picture_description=False (default): bật khi có VLM backend để mô tả ảnh/diagram.
  - Lazy-load: DocumentConverter chỉ khởi tạo khi parse() được gọi lần đầu.

Đây là backend đắt (OCR + layout model). ``parse_pages()`` chạy Docling trên
một khoảng trang để ``AdaptivePdfParser`` chỉ trả tiền cho những trang mà text
path không đọc được, thay vì OCR lại cả tài liệu.

Kết quả luôn kèm ``metadata['page_report']`` theo cùng contract với
``pdf_page_assessment`` để coverage của hai backend ghép được với nhau.
"""
import io
import uuid
from functools import cached_property
from pathlib import Path

from document_chunk.domain.entities.document import (
    Document,
    DocumentType,
    ElementType,
    ParsedDocument,
    Section,
)
from document_chunk.adapters.parsers.pdf_page_assessment import PageStatus
from document_chunk.domain.exceptions import ParseError
from document_chunk.domain.ports.parser import IParser
from document_chunk.infrastructure.config import ParserConfig
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

PARSER_NAME = "docling"


class DoclingPdfParser(IParser):
    def __init__(self, config: ParserConfig) -> None:
        self._config = config

    @property
    def supported_types(self) -> tuple[DocumentType, ...]:
        return (DocumentType.PDF,)

    # ------------------------------------------------------------------
    # Public API — IParser
    # ------------------------------------------------------------------

    def parse(self, path: Path) -> Result[ParsedDocument, Exception]:
        return self._run(path, page_range=None)

    def parse_pages(
        self, path: Path, first_page: int, last_page: int
    ) -> Result[ParsedDocument, Exception]:
        """Chạy Docling trên khoảng trang [first_page, last_page] (1-based, inclusive)."""
        return self._run(path, page_range=(first_page, last_page))

    def _run(
        self, path: Path, *, page_range: tuple[int, int] | None
    ) -> Result[ParsedDocument, Exception]:
        logger.info("docling_pdf_parser.started", file=path.name, page_range=page_range)
        with tracer.start_as_current_span("docling_pdf_parser.parse") as span:
            span.set_attribute("file.name", path.name)
            span.set_attribute("file.size_bytes", path.stat().st_size)
            if page_range:
                span.set_attribute("page_range", f"{page_range[0]}-{page_range[1]}")
            try:
                kwargs = {"page_range": page_range} if page_range else {}
                result = self._converter.convert(str(path), **kwargs)
                parsed = self._extract(path, result, page_range=page_range)
                span.set_attribute("pages", parsed.page_count)
                span.set_attribute("sections", len(parsed.sections))
                span.set_attribute("images", len(parsed.images))
                logger.info(
                    "docling_pdf_parser.completed",
                    file=path.name,
                    page_range=page_range,
                    pages=parsed.page_count,
                    sections=len(parsed.sections),
                    images=len(parsed.images),
                )
                return Ok(parsed)
            except Exception as exc:
                logger.error(
                    "docling_pdf_parser.failed",
                    file=path.name,
                    page_range=page_range,
                    error=str(exc),
                )
                return Err(ParseError(f"Failed to parse PDF '{path.name}'", cause=exc))

    # ------------------------------------------------------------------
    # Lazy converter — khởi tạo DocumentConverter khi lần đầu được dùng
    # ------------------------------------------------------------------

    @cached_property
    def _converter(self):  # type: ignore[return]
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as exc:
            raise ImportError(
                "docling chưa được cài. Chạy: uv add docling"
            ) from exc

        pipeline_options = self._build_pipeline_options()
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
        logger.info(
            "docling_pdf_parser.converter_initialized",
            do_ocr=self._config.docling_do_ocr,
            do_table_structure=self._config.docling_do_table_structure,
            do_picture_description=self._config.docling_do_picture_description,
        )
        return converter

    # ------------------------------------------------------------------
    # Pipeline options
    # ------------------------------------------------------------------

    def _build_pipeline_options(self):
        from docling.datamodel.pipeline_options import PdfPipelineOptions

        opts = PdfPipelineOptions()
        opts.do_ocr = self._config.docling_do_ocr
        # auto-detect: do_ocr=True + force_full_page_ocr=False
        # → dùng text layer nếu tốt, chỉ OCR những trang thiếu text layer
        opts.ocr_options.force_full_page_ocr = False
        opts.do_table_structure = self._config.docling_do_table_structure
        opts.do_picture_description = self._config.docling_do_picture_description

        if self._config.docling_do_picture_description:
            opts = self._attach_picture_description_backend(opts)

        return opts

    def _attach_picture_description_backend(self, opts):
        backend = self._config.docling_picture_description_backend
        try:
            from docling.datamodel.pipeline_options import (
                PictureDescriptionApiOptions,
                PictureDescriptionVlmOptions,
            )
        except ImportError:
            logger.warning(
                "docling_pdf_parser.picture_description_import_failed",
                backend=backend,
            )
            opts.do_picture_description = False
            return opts

        if backend == "granite":
            opts.picture_description_options = PictureDescriptionVlmOptions()
        elif backend == "openai_api":
            opts.picture_description_options = PictureDescriptionApiOptions()
        else:
            logger.warning(
                "docling_pdf_parser.unknown_picture_backend", backend=backend
            )
            opts.do_picture_description = False

        return opts

    # ------------------------------------------------------------------
    # Core extraction: ConversionResult → ParsedDocument
    # ------------------------------------------------------------------

    def _extract(
        self, path: Path, result, *, page_range: tuple[int, int] | None = None
    ) -> ParsedDocument:
        doc = result.document  # DoclingDocument
        page_count = self._page_count(result, doc)

        keep_image_bytes = self._config.pdf_extract_images
        sections = self._iter_sections(doc, keep_image_bytes=keep_image_bytes)
        images = self._extract_images(doc) if keep_image_bytes else {}

        # Guard: Docling trả về empty → file hỏng hoặc blank PDF
        if not sections and not images:
            raise ValueError(
                f"Không extract được nội dung từ '{path.name}'. "
                "File có thể bị hỏng hoặc hoàn toàn trống."
            )

        # Enforce pdf_max_pages nếu có
        if self._config.pdf_max_pages is not None:
            limit = self._config.pdf_max_pages
            sections = [
                s for s in sections
                if s.page_number is None or s.page_number <= limit
            ]

        document = Document(
            id=str(uuid.uuid4()),
            name=path.name,
            path=path,
            doc_type=DocumentType.PDF,
            size_bytes=path.stat().st_size,
            mime_type="application/pdf",
        )

        # Language: lấy từ Docling metadata nếu có
        language: str | None = None
        if hasattr(doc, "metadata") and doc.metadata:
            language = getattr(doc.metadata, "language", None)

        report = self._page_report(sections, page_count, page_range)
        return ParsedDocument(
            document=document,
            sections=sections,
            page_count=page_count,
            images=images,
            language=language,
            metadata={
                "parser": PARSER_NAME,
                "do_ocr": self._config.docling_do_ocr,
                "do_table_structure": self._config.docling_do_table_structure,
                "extract_images": keep_image_bytes,
                "page_range": list(page_range) if page_range else None,
                "page_report": report,
            },
        )

    # ------------------------------------------------------------------
    # Page bookkeeping
    # ------------------------------------------------------------------

    def _page_count(self, result, doc) -> int:
        """Số trang của tài liệu — không phải số trang đã convert."""
        try:
            count = doc.num_pages()
            if count:
                return int(count)
        except Exception:
            pass
        return len(result.pages) if result.pages else 0

    def _page_report(
        self,
        sections: list[Section],
        page_count: int,
        page_range: tuple[int, int] | None,
    ) -> list[dict]:
        """
        Một dòng mỗi trang đã convert, cùng contract với pdf_page_assessment.

        Trang đã qua Docling mà vẫn không có nội dung → ``needs_ocr`` với
        ``parsed_by=None``: đã xử lý nhưng không ra text (ảnh không chứa chữ),
        khác với trang chưa ai đọc.
        """
        chars: dict[int, int] = {}
        for section in sections:
            if section.page_number is None:
                continue
            chars[section.page_number] = chars.get(section.page_number, 0) + len(
                section.content
            )

        # Docling giữ số trang tuyệt đối khi convert theo khoảng
        # (standard_pdf_pipeline: Page(page_no=i + 1)), nên không cần dịch offset.
        first, last = page_range or (1, page_count)
        pages = range(first, last + 1) if last >= first else sorted(chars)
        report: list[dict] = []
        for page in pages:
            count = chars.get(page, 0)
            report.append({
                "page": page,
                "status": (
                    PageStatus.TEXT.value if count else PageStatus.NEEDS_OCR.value
                ),
                "chars": count,
                "lines": 0,
                "image_area_ratio": 0.0,
                "multi_column": False,
                "tables": 0,
                "reasons": [] if count else ["no_content_from_docling"],
                "attempts": [PARSER_NAME],
                "parsed_by": PARSER_NAME if count else None,
                "layout_aware": bool(count),
            })
        return report

    # ------------------------------------------------------------------
    # Section iteration — dispatch theo item type
    # ------------------------------------------------------------------

    def _iter_sections(self, doc, *, keep_image_bytes: bool = True) -> list[Section]:
        # Kiểu item nằm ở docling_core; docling.datamodel.document chỉ re-export
        # và đã bỏ ListItem ở docling 2.118.
        from docling_core.types.doc import (
            ListItem,
            PictureItem,
            SectionHeaderItem,
            TableItem,
            TextItem,
        )

        sections: list[Section] = []
        current_heading: str | None = None
        current_heading_level: int = 0

        for item, _level in doc.iterate_items():
            section: Section | None = None

            if isinstance(item, SectionHeaderItem):
                section = self._map_section_header(item)
                # Cập nhật heading context cho các item tiếp theo
                current_heading = section.content
                current_heading_level = section.heading_level
            elif isinstance(item, TableItem):
                section = self._map_table_item(item, current_heading, current_heading_level)
            elif isinstance(item, PictureItem):
                section = self._map_picture_item(
                    item, doc, current_heading, current_heading_level,
                    keep_image_bytes=keep_image_bytes,
                )
            elif isinstance(item, ListItem):
                section = self._map_list_item(item, current_heading, current_heading_level)
            elif isinstance(item, TextItem):
                section = self._map_text_item(item, current_heading, current_heading_level)
            # Các item type khác (footnote, header/footer, v.v.) → bỏ qua

            if section is not None:
                sections.append(section)

        return sections

    # ------------------------------------------------------------------
    # Per-item mapping
    # ------------------------------------------------------------------

    def _map_section_header(self, item) -> Section:
        text = item.text.strip()
        # SectionHeaderItem.level: int, ge=1, le=100 — semantic heading level
        heading_level = max(1, min(item.level, 6))
        page_number = item.prov[0].page_no if item.prov else None

        return Section(
            content=text,
            element_type=ElementType.HEADING,
            heading=text,
            heading_level=heading_level,
            page_number=page_number,
            metadata={"docling_type": "SectionHeaderItem"},
        )

    def _map_text_item(
        self, item, current_heading: str | None, current_heading_level: int
    ) -> Section | None:
        text = item.text.strip()
        if not text:
            return None
        page_number = item.prov[0].page_no if item.prov else None

        return Section(
            content=text,
            element_type=ElementType.PARAGRAPH,
            heading=current_heading,
            heading_level=current_heading_level,
            page_number=page_number,
            metadata={"docling_type": "TextItem"},
        )

    def _map_table_item(
        self, item, current_heading: str | None, current_heading_level: int
    ) -> Section | None:
        # Ưu tiên markdown (giữ cấu trúc cột/hàng); fallback sang HTML nếu lỗi
        content: str = ""
        try:
            content = item.export_to_markdown()
        except Exception:
            try:
                content = item.export_to_html()
            except Exception:
                return None

        if not content.strip():
            return None

        page_number = item.prov[0].page_no if item.prov else None

        return Section(
            content=content,
            element_type=ElementType.TABLE,
            heading=current_heading,
            heading_level=current_heading_level,
            page_number=page_number,
            metadata={"docling_type": "TableItem"},
        )

    def _map_picture_item(
        self,
        item,
        doc,
        current_heading: str | None,
        current_heading_level: int,
        *,
        keep_image_bytes: bool = True,
    ) -> Section | None:
        img_ref = f"img_{id(item)}.png"

        # Caption từ text gần ảnh
        caption = ""
        try:
            caption = item.caption_text(doc) or ""
        except Exception:
            pass

        # VLM description (chỉ có nếu do_picture_description=True)
        description = ""
        if self._config.docling_do_picture_description:
            try:
                from docling_core.types.doc.document import DescriptionAnnotation
                for ann in item.get_annotations():
                    if isinstance(ann, DescriptionAnnotation) and ann.text:
                        description = ann.text
                        break
            except Exception:
                pass

        # Content: description ưu tiên hơn caption
        content = description or caption or "[Image]"
        page_number = item.prov[0].page_no if item.prov else None

        return Section(
            content=content,
            element_type=ElementType.IMAGE,
            heading=current_heading,
            heading_level=current_heading_level,
            page_number=page_number,
            images=(img_ref,) if keep_image_bytes else (),
            metadata={
                "docling_type": "PictureItem",
                "caption": caption,
                "has_description": bool(description),
            },
        )

    def _map_list_item(
        self, item, current_heading: str | None, current_heading_level: int
    ) -> Section | None:
        text = item.text.strip()
        if not text:
            return None
        page_number = item.prov[0].page_no if item.prov else None

        return Section(
            content=text,
            element_type=ElementType.LIST,
            heading=current_heading,
            heading_level=current_heading_level,
            page_number=page_number,
            metadata={"docling_type": "ListItem"},
        )

    # ------------------------------------------------------------------
    # Image bytes extraction
    # ------------------------------------------------------------------

    def _extract_images(self, doc) -> dict[str, bytes]:
        from docling_core.types.doc import PictureItem

        images: dict[str, bytes] = {}
        for item, _ in doc.iterate_items():
            if not isinstance(item, PictureItem):
                continue
            img_ref = f"img_{id(item)}.png"
            try:
                pil_img = item.get_image(doc)
                if pil_img is not None:
                    buf = io.BytesIO()
                    pil_img.save(buf, format="PNG")
                    images[img_ref] = buf.getvalue()
            except Exception as exc:
                logger.warning(
                    "docling_pdf_parser.image_extraction_failed",
                    item_id=id(item),
                    error=str(exc),
                )
        return images

"""
MarkdownParser — parse Markdown (.md/.markdown) -> ParsedDocument.

Strategy:
  - Heading lines (#, ##, ..., ######) -> Section(HEADING)
  - Content dưới heading -> Section(PARAGRAPH) với heading context
  - Paragraph tách theo dòng trống
"""
import re
import uuid
from pathlib import Path

from document_chunk.domain.entities.document import (
    Document,
    DocumentType,
    ElementType,
    ParsedDocument,
    Section,
)
from document_chunk.domain.exceptions import ParseError
from document_chunk.domain.ports.parser import IParser
from document_chunk.infrastructure.config import ParserConfig
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


class MarkdownParser(IParser):
    def __init__(self, config: ParserConfig) -> None:
        self._config = config

    @property
    def supported_types(self) -> tuple[DocumentType, ...]:
        return (DocumentType.MARKDOWN,)

    def parse(self, path: Path) -> Result[ParsedDocument, Exception]:
        logger.info("markdown_parser.started", file=path.name)
        with tracer.start_as_current_span("markdown_parser.parse") as span:
            span.set_attribute("file.name", path.name)
            span.set_attribute("file.size_bytes", path.stat().st_size)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                result = self._extract(path, text)
                span.set_attribute("sections", len(result.sections))
                logger.info(
                    "markdown_parser.completed",
                    file=path.name,
                    sections=len(result.sections),
                )
                return Ok(result)
            except Exception as e:
                logger.error("markdown_parser.failed", file=path.name, error=str(e))
                return Err(ParseError(f"Failed to parse Markdown '{path.name}'", cause=e))

    def _extract(self, path: Path, text: str) -> ParsedDocument:
        sections: list[Section] = []
        current_lines: list[str] = []
        current_heading: str | None = None
        current_heading_level = 0
        fence = None
        block_type = ElementType.PARAGRAPH

        def flush_paragraph() -> None:
            content = "\n".join(current_lines).strip()
            if content:
                sections.append(
                    Section(
                        content=content,
                        element_type=block_type,
                        heading=current_heading,
                        heading_level=current_heading_level,
                    )
                )
            current_lines.clear()

        # Front matter is metadata, not document content.
        text = re.sub(r"\A---[ \t]*\n.*?\n(?:---|\.\.\.)[ \t]*(?:\n|$)", "", text, count=1, flags=re.S)
        lines = text.splitlines()
        for line_index, raw_line in enumerate(lines):
            line = raw_line.rstrip()
            stripped = line.strip()

            marker = re.match(r"^(`{3,}|~{3,})(.*)$", stripped)
            if fence:
                current_lines.append(raw_line)
                if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                    flush_paragraph()
                    fence = None
                    block_type = ElementType.PARAGRAPH
                continue
            if marker:
                flush_paragraph()
                fence = marker[1]
                block_type = ElementType.CODE
                current_lines.append(raw_line)
                continue

            m = _HEADING_RE.match(stripped)
            if m:
                flush_paragraph()
                heading_level = len(m.group(1))
                heading_text = m.group(2).strip()
                sections.append(
                    Section(
                        content=heading_text,
                        element_type=ElementType.HEADING,
                        heading=heading_text,
                        heading_level=heading_level,
                    )
                )
                current_heading = heading_text
                current_heading_level = heading_level
                continue

            if not stripped:
                flush_paragraph()
                block_type = ElementType.PARAGRAPH
                continue

            is_table = "|" in line and (
                block_type == ElementType.TABLE or
                (line_index + 1 < len(lines) and re.fullmatch(r"[\s|:~-]+", lines[line_index + 1]) is not None)
            )
            is_list = re.match(r"^\s*(?:[-*+•▪–]|\d+[.)]|[a-zđ][.)])\s+", line)
            kind = ElementType.TABLE if is_table else ElementType.LIST if is_list else ElementType.PARAGRAPH
            if block_type == ElementType.LIST and line.startswith(("  ", "\t")):
                kind = ElementType.LIST
            if kind != block_type:
                flush_paragraph()
                block_type = kind
            current_lines.append(line)

        flush_paragraph()

        # File markdown có thể không có heading, giữ nguyên toàn bộ thành 1 section.
        if not sections and text.strip():
            sections.append(
                Section(
                    content=text.strip(),
                    element_type=ElementType.PARAGRAPH,
                )
            )

        document = Document(
            id=str(uuid.uuid4()),
            name=path.name,
            path=path,
            doc_type=DocumentType.MARKDOWN,
            size_bytes=path.stat().st_size,
            mime_type="text/markdown",
        )

        return ParsedDocument(
            document=document,
            sections=sections,
            page_count=0,
            metadata={"parser": "markdown"},
        )

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

from src.domain.entities.document import (
    Document,
    DocumentType,
    ElementType,
    ParsedDocument,
    Section,
)
from src.domain.ports.parser import IParser
from src.infrastructure.config import ParserConfig
from src.shared.logger import get_logger
from src.shared.result import Err, Ok, Result

logger = get_logger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


class MarkdownParser(IParser):
    def __init__(self, config: ParserConfig) -> None:
        self._config = config

    @property
    def supported_types(self) -> tuple[DocumentType, ...]:
        return (DocumentType.MARKDOWN,)

    def parse(self, path: Path) -> Result[ParsedDocument, Exception]:
        logger.info("markdown_parser.started", file=path.name)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            result = self._extract(path, text)
            logger.info(
                "markdown_parser.completed",
                file=path.name,
                sections=len(result.sections),
            )
            return Ok(result)
        except Exception as e:
            logger.error("markdown_parser.failed", file=path.name, error=str(e))
            return Err(e)

    def _extract(self, path: Path, text: str) -> ParsedDocument:
        sections: list[Section] = []
        current_lines: list[str] = []
        current_heading: str | None = None
        current_heading_level = 0

        def flush_paragraph() -> None:
            content = "\n".join(current_lines).strip()
            if content:
                sections.append(
                    Section(
                        content=content,
                        element_type=ElementType.PARAGRAPH,
                        heading=current_heading,
                        heading_level=current_heading_level,
                    )
                )
            current_lines.clear()

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()

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
                continue

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

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class DocumentType(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    MARKDOWN = "markdown"


class ElementType(str, Enum):
    """Loại element trong document — preserve từ source, không qua Markdown."""
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    LIST = "list"
    SLIDE = "slide"       # PPTX: một slide là một element
    IMAGE = "image"
    CODE = "code"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Document:
    """
    Represents a raw document file — input của pipeline.
    Immutable: chỉ là metadata, không chứa content.
    """
    id: str
    name: str
    path: Path
    doc_type: DocumentType
    size_bytes: int
    mime_type: str
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class Section:
    """
    Một đơn vị nội dung có cấu trúc — output trực tiếp từ parser.

    Thay vì flatten sang Markdown, mỗi parser giữ nguyên structure
    của format gốc:
      - DOCX: section = paragraph/heading từ Word styles
      - PPTX: section = một slide (title + body + notes)
      - PDF:  section = block từ PyMuPDF (detected bằng font size/position)
      - MD:   section = heading-delimited block
    """
    content: str
    element_type: ElementType
    heading: str | None = None       # text của heading trực tiếp chứa section này
    heading_level: int = 0           # 0 = không có heading, 1-6 = H1-H6
    page_number: int | None = None   # trang trong document gốc
    images: tuple[str, ...] = ()     # filenames của ảnh trong section này
    metadata: dict = field(default_factory=dict)  # extra info từng format


@dataclass
class ParsedDocument:
    """
    Kết quả sau khi parse document — input của Chunker.
    Giữ nguyên structure, không flatten sang Markdown.
    """
    document: Document
    sections: list[Section]
    page_count: int
    images: dict[str, bytes] = field(default_factory=dict)  # filename → raw bytes
    language: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def total_content_length(self) -> int:
        return sum(len(s.content) for s in self.sections)

    @property
    def headings(self) -> list[str]:
        """Danh sách tất cả headings trong document — dùng để debug."""
        return [s.heading for s in self.sections if s.heading]

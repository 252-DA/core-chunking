"""Domain-level constants — single source of truth for file type mappings."""
from document_chunk.domain.entities.document import DocumentType

# File extension → DocumentType
EXT_MAP: dict[str, DocumentType] = {
    ".pdf": DocumentType.PDF,
    ".docx": DocumentType.DOCX,
    ".doc": DocumentType.DOCX,
    ".pptx": DocumentType.PPTX,
    ".ppt": DocumentType.PPTX,
    ".md": DocumentType.MARKDOWN,
    ".markdown": DocumentType.MARKDOWN,
}

# DocumentType → MIME type
MIME_MAP: dict[DocumentType, str] = {
    DocumentType.PDF: "application/pdf",
    DocumentType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    DocumentType.PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    DocumentType.MARKDOWN: "text/markdown",
}

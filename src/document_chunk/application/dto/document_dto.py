from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from document_chunk.domain.entities.document import DocumentType


class ProcessDocumentRequest(BaseModel):
    """
    Input của ProcessDocumentUseCase.
    Validate tại boundary — delivery layer gửi vào.
    """
    file_path: Path
    document_id: str | None = None          # None → auto-generate UUID
    original_file_name: str | None = None   # Tên file gốc từ client (không phải temp file)
    doc_type: DocumentType | None = None    # None → detect từ extension
    language: str | None = None             # None → auto-detect
    metadata: dict = Field(default_factory=dict)

    @field_validator("file_path")
    @classmethod
    def file_must_exist(cls, v: Path) -> Path:
        if not v.exists():
            raise ValueError(f"File not found: {v}")
        if not v.is_file():
            raise ValueError(f"Path is not a file: {v}")
        return v


class ChunkSummary(BaseModel):
    """Tóm tắt một chunk — không trả full content để nhẹ response."""
    chunk_id: str
    heading_path: list[str]
    content_preview: str       # 200 ký tự đầu
    content_length: int
    page_number: int | None
    has_images: bool


class ProcessDocumentResponse(BaseModel):
    """
    Output của ProcessDocumentUseCase.
    """
    document_id: str
    document_name: str
    doc_type: DocumentType
    chunk_count: int
    chunks: list[ChunkSummary]
    storage_key: str            # MinIO key của raw file
    processing_time_ms: float

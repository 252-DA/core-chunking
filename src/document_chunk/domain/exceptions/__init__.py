"""
Domain exceptions — typed errors cho từng layer trong pipeline.

Hierarchy:
    ChunkingError (base)
    ├── ParseError               — parser không đọc được file
    │   └── UnsupportedFileTypeError  — không có parser cho loại file này
    ├── ChunkError               — chunker thất bại
    ├── EmbedError               — embedder thất bại
    ├── VectorStoreError         — lỗi lúc upsert / search Qdrant
    ├── FileStorageError         — lỗi lúc upload / download MinIO
    ├── MetadataStoreError       — lỗi lúc đọc / ghi PostgreSQL
    ├── GraphStoreError          — lỗi lúc đọc / ghi Neo4j
    └── ProcessingError          — lỗi orchestration (use case level)

Cách dùng trong adapters:
    except SomeSDKError as e:
        return Err(ParseError("pdf_parser failed", cause=e))

Cách dùng trong use cases:
    if result.is_err():
        raise ProcessingError("pipeline failed", cause=result.error)
"""
from __future__ import annotations


class ChunkingError(Exception):
    """Base exception cho toàn bộ pipeline."""

    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause

    def __str__(self) -> str:
        if self.cause:
            return f"{super().__str__()} (caused by: {self.cause})"
        return super().__str__()


# ---------------------------------------------------------------------------
# Parser layer
# ---------------------------------------------------------------------------

class ParseError(ChunkingError):
    """Parser không đọc được file — file corrupt, format lạ, v.v."""


class UnsupportedFileTypeError(ParseError):
    """Không tìm được parser nào hỗ trợ loại file này."""

    def __init__(self, file_type: str) -> None:
        super().__init__(f"No parser available for file type: {file_type!r}")
        self.file_type = file_type


# ---------------------------------------------------------------------------
# Chunker layer
# ---------------------------------------------------------------------------

class ChunkError(ChunkingError):
    """Chunker thất bại khi chia document thành chunks."""


# ---------------------------------------------------------------------------
# Embedder layer
# ---------------------------------------------------------------------------

class EmbedError(ChunkingError):
    """Embedder thất bại — model lỗi, input quá dài, v.v."""


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------

class VectorStoreError(ChunkingError):
    """Lỗi khi tương tác với vector database (Qdrant)."""


class FileStorageError(ChunkingError):
    """Lỗi khi tương tác với object storage (MinIO / S3)."""


class MetadataStoreError(ChunkingError):
    """Lỗi khi tương tác với metadata database (PostgreSQL)."""


class DocumentStaleError(MetadataStoreError):
    """
    Document không tồn tại hoặc đã bị soft-delete khi worker đang ghi.

    Khác MetadataStoreError thường: đây KHÔNG phải lỗi hạ tầng — pipeline phải
    dừng ngay và không được ghi Qdrant/chunks/outbox, để tránh hồi sinh dữ liệu
    của document đã xóa (delete race).
    """

    def __init__(self, message: str, document_id: str | None = None) -> None:
        super().__init__(message)
        self.document_id = document_id


class GraphStoreError(ChunkingError):
    """Lỗi khi tương tác với graph database (Neo4j)."""


class LLMError(ChunkingError):
    """Lỗi khi tương tác với text generation model/provider."""


# ---------------------------------------------------------------------------
# Use case / orchestration layer
# ---------------------------------------------------------------------------

class ProcessingError(ChunkingError):
    """
    Lỗi orchestration — wrap lỗi từ các layer bên dưới khi cần
    expose ra delivery layer với context rõ hơn.
    """


__all__ = [
    "ChunkingError",
    "ParseError",
    "UnsupportedFileTypeError",
    "ChunkError",
    "EmbedError",
    "VectorStoreError",
    "FileStorageError",
    "MetadataStoreError",
    "DocumentStaleError",
    "GraphStoreError",
    "LLMError",
    "ProcessingError",
]

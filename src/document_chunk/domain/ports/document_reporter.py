"""
Báo trạng thái xử lý và chương suy luận của một tài liệu.

`documents` là bảng của core-api. Use case trong packages-ai không ghi thẳng
bảng đó mà báo qua hai port nhỏ này; worker cài đặt bằng cách gửi kết quả
DOCUMENT_PROCESSING_RESULT qua hàng đợi ``worker_results`` để core-api ghi
(xem ``worker/services/document_reporter.py`` và
``docs/ke-hoach-ranh-gioi-du-lieu.md``).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from document_chunk.domain.ports.metadata_store import IngestionStatus
from document_chunk.shared.result import Result


class IDocumentStatusReporter(ABC):
    """Một lượt xử lý báo từng chuyển trạng thái của tài liệu."""

    @abstractmethod
    def report_status(
        self,
        document_id: str,
        status: IngestionStatus,
        error_msg: str | None = None,
    ) -> Result[None, Exception]:
        """
        Err(DocumentStaleError) khi tài liệu không còn (đã xoá): người gọi dừng
        và không ghi dữ liệu dẫn xuất. Err khác: báo không đi được, lượt xử lý
        phải lỗi để được chạy lại.
        """
        ...


class IDocumentChapterReporter(ABC):
    """Chương suy ra từ nội dung tài liệu (MapChunksToLosUseCase)."""

    @abstractmethod
    def report_chapter(
        self,
        document_id: str,
        chapter_code: str,
        provenance: str,
        confidence: float,
        reason: str,
    ) -> Result[None, Exception]:
        """Nơi ghi tự giữ quy tắc: không đè chương giảng viên đã xác nhận."""
        ...

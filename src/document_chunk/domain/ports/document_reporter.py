"""
Báo trạng thái xử lý và chương suy luận của một tài liệu.

`documents` là bảng của core-api. Use case trong packages-ai không ghi thẳng
bảng đó nữa mà báo qua hai port nhỏ này; nơi lắp ráp chọn cài đặt:

- worker (stack compose hiện tại): gửi kết quả DOCUMENT_PROCESSING_RESULT qua
  hàng đợi ``worker_results``, core-api ghi (worker/services/document_reporter.py);
- delivery gRPC/HTTP cũ của packages-ai (profile "legacy", không chạy mặc định):
  ``MetadataStoreDocumentReporter`` ghi thẳng như trước, để đường cũ không đổi
  hành vi cho tới khi bị xoá. Xem docs/ke-hoach-ranh-gioi-du-lieu.md.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from document_chunk.domain.ports.metadata_store import IMetadataStore, IngestionStatus
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Ok, Result

logger = get_logger(__name__)


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


class MetadataStoreDocumentReporter(IDocumentStatusReporter, IDocumentChapterReporter):
    """
    Ghi thẳng `documents` qua IMetadataStore — hành vi cũ, chỉ cho delivery
    gRPC/HTTP của packages-ai và script. Worker không dùng lớp này.

    LEGACY: xoá cùng delivery legacy và ba hàm ghi ``documents`` trong
    PostgresDocumentRepository / PostgresDocumentPlacementRepository.
    """

    def __init__(self, metadata_store: IMetadataStore) -> None:
        self._metadata_store = metadata_store

    def report_status(
        self,
        document_id: str,
        status: IngestionStatus,
        error_msg: str | None = None,
    ) -> Result[None, Exception]:
        return self._metadata_store.update_document_status(
            document_id=document_id, status=status, error_msg=error_msg
        )

    def report_chapter(
        self,
        document_id: str,
        chapter_code: str,
        provenance: str,
        confidence: float,
        reason: str,
    ) -> Result[None, Exception]:
        result = self._metadata_store.set_document_chapter(
            document_id, chapter_code, provenance, confidence, reason
        )
        if result.is_err():
            # Hành vi cũ: ghi chương lỗi không chặn việc map chunk → LO.
            logger.warning(
                "document_reporter.set_chapter_failed",
                document_id=document_id,
                error=str(result.error),
            )
        return Ok(None)

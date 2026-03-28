from abc import ABC, abstractmethod
from pathlib import Path

from src.shared.result import Result


class IFileStorage(ABC):
    """
    Port: lưu trữ raw files và images.
    Trước mắt: MinIO. Có thể swap sang S3, GCS, local disk.
    """

    @abstractmethod
    def upload(self, local_path: Path, remote_key: str) -> Result[str, Exception]:
        """
        Upload file lên storage.
        Trả về Ok(remote_key) hoặc Err(exception).
        """
        ...

    @abstractmethod
    def download(self, remote_key: str, local_path: Path) -> Result[None, Exception]:
        """Download file từ storage về local_path."""
        ...

    @abstractmethod
    def delete(self, remote_key: str) -> Result[None, Exception]:
        """Xóa file trên storage."""
        ...

    @abstractmethod
    def exists(self, remote_key: str) -> bool:
        """Kiểm tra file có tồn tại không."""
        ...

    @abstractmethod
    def get_url(self, remote_key: str, expires_in: int = 3600) -> Result[str, Exception]:
        """
        Tạo presigned URL để truy cập file.
        expires_in: seconds.
        """
        ...

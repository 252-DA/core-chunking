"""
MinioAdapter — implement IFileStorage dùng MinIO (S3-compatible).

Design:
  - Lazy connect qua @cached_property _client
  - _ensure_bucket: tạo bucket nếu chưa tồn tại khi connect lần đầu
  - Tất cả methods wrap SDK calls trong try/except → Result[T, Exception]
  - presigned_get_object cho URL có thời hạn (expires_in seconds)
"""
from datetime import timedelta
from functools import cached_property
from pathlib import Path

from minio import Minio
from minio.error import S3Error

from src.domain.exceptions import FileStorageError
from src.domain.ports.file_storage import IFileStorage
from src.infrastructure.config import MinioConfig
from src.shared.logger import get_logger
from src.shared.result import Err, Ok, Result
from src.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)


class MinioAdapter(IFileStorage):
    def __init__(self, config: MinioConfig) -> None:
        self._config = config

    @cached_property
    def _client(self) -> Minio:
        """Lazy connect — chỉ kết nối khi cần."""
        client = Minio(
            endpoint=self._config.endpoint,
            access_key=self._config.access_key,
            secret_key=self._config.secret_key,
            secure=self._config.secure,
        )
        logger.info(
            "minio.connected",
            endpoint=self._config.endpoint,
            bucket=self._config.bucket_name,
        )
        self._ensure_bucket(client)
        return client

    # ------------------------------------------------------------------
    # IFileStorage implementation
    # ------------------------------------------------------------------

    def upload(self, local_path: Path, remote_key: str) -> Result[str, Exception]:
        with tracer.start_as_current_span("minio.upload") as span:
            span.set_attribute("remote_key", remote_key)
            span.set_attribute("local_path", str(local_path))

            try:
                self._client.fput_object(
                    bucket_name=self._config.bucket_name,
                    object_name=remote_key,
                    file_path=str(local_path),
                )
                logger.info(
                    "minio.uploaded",
                    key=remote_key,
                    size_bytes=local_path.stat().st_size,
                )
                return Ok(remote_key)

            except Exception as e:
                logger.error("minio.upload.failed", key=remote_key, error=str(e))
                return Err(FileStorageError(f"MinIO upload failed for key '{remote_key}'", cause=e))

    def download(self, remote_key: str, local_path: Path) -> Result[None, Exception]:
        with tracer.start_as_current_span("minio.download") as span:
            span.set_attribute("remote_key", remote_key)

            try:
                self._client.fget_object(
                    bucket_name=self._config.bucket_name,
                    object_name=remote_key,
                    file_path=str(local_path),
                )
                logger.info("minio.downloaded", key=remote_key, dest=str(local_path))
                return Ok(None)

            except Exception as e:
                logger.error("minio.download.failed", key=remote_key, error=str(e))
                return Err(FileStorageError(f"MinIO download failed for key '{remote_key}'", cause=e))

    def delete(self, remote_key: str) -> Result[None, Exception]:
        try:
            self._client.remove_object(
                bucket_name=self._config.bucket_name,
                object_name=remote_key,
            )
            logger.info("minio.deleted", key=remote_key)
            return Ok(None)

        except Exception as e:
            logger.error("minio.delete.failed", key=remote_key, error=str(e))
            return Err(FileStorageError(f"MinIO delete failed for key '{remote_key}'", cause=e))

    def exists(self, remote_key: str) -> bool:
        try:
            self._client.stat_object(
                bucket_name=self._config.bucket_name,
                object_name=remote_key,
            )
            return True
        except S3Error:
            return False

    def get_url(
        self, remote_key: str, expires_in: int = 3600
    ) -> Result[str, Exception]:
        try:
            url = self._client.presigned_get_object(
                bucket_name=self._config.bucket_name,
                object_name=remote_key,
                expires=timedelta(seconds=expires_in),
            )
            logger.debug("minio.presigned_url", key=remote_key, expires_in=expires_in)
            return Ok(url)

        except Exception as e:
            logger.error("minio.get_url.failed", key=remote_key, error=str(e))
            return Err(FileStorageError(f"MinIO get_url failed for key '{remote_key}'", cause=e))

    # ------------------------------------------------------------------
    # Bucket management
    # ------------------------------------------------------------------

    def _ensure_bucket(self, client: Minio) -> None:
        """Tạo bucket nếu chưa tồn tại."""
        name = self._config.bucket_name
        if not client.bucket_exists(name):
            client.make_bucket(name)
            logger.info("minio.bucket.created", name=name)
        else:
            logger.info("minio.bucket.exists", name=name)

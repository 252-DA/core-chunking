"""GrpcEmbedder — IEmbedder adapter backed by embedding-service over gRPC.

This is the only module in packages-ai that imports da_service_sdk — every
other module keeps talking to IEmbedder, unaware embedding moved out of
process. See docs/embedding-service-migration.md §9 step 8 in da-platform.

The adapter performs the model-info/dimension check at startup, translates SDK
exceptions to the domain Result type, and owns the client's channel lifetime.
"""

from document_chunk.domain.exceptions import EmbedError
from document_chunk.domain.ports.embedder import IEmbedder
from document_chunk.infrastructure.config import EmbedderConfig
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result
from da_service_sdk.client import EmbeddingClient
from da_service_sdk.exceptions import EmbeddingServiceError

logger = get_logger(__name__)


class GrpcEmbedder(IEmbedder):
    def __init__(self, config: EmbedderConfig, expected_dimension: int = 1024) -> None:
        self._config = config
        self._client = EmbeddingClient(config.grpc_target)
        try:
            self._model_info = self._client.get_model_info()
        except Exception:
            self._client.close()
            raise
        if self._model_info.dimension != expected_dimension:
            self._client.close()
            raise ValueError(
                "embedding-service dimension does not match the configured vector "
                f"size: {self._model_info.dimension} != {expected_dimension}"
            )

    @property
    def model_name(self) -> str:
        return self._model_info.model_name

    @property
    def dimension(self) -> int:
        return self._model_info.dimension

    def embed(self, texts: list[str]) -> Result[list[list[float]], Exception]:
        if not texts:
            return Ok([])

        try:
            return Ok(self._client.embed(texts).vectors)
        except EmbeddingServiceError as exc:
            logger.error("grpc_embedder.failed", target=self._config.grpc_target, error=str(exc))
            return Err(EmbedError("Remote embedding service failed", cause=exc))

    def close(self) -> None:
        self._client.close()

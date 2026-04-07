"""
BgeEmbedder — tạo vector embeddings dùng BAAI/bge-m3 (FlagEmbedding).

BGE-M3 đặc điểm:
  - 1024 dimensions
  - Multilingual (Vietnamese tốt)
  - Hỗ trợ dense, sparse, colbert — dùng dense ở đây
  - Max token length: 8192

Model load một lần duy nhất (lazy, singleton qua container).
"""
import time
from functools import cached_property

from src.domain.exceptions import EmbedError
from src.domain.ports.embedder import IEmbedder
from src.infrastructure.config import EmbedderConfig
from src.shared.logger import get_logger
from src.shared.metrics import EMBEDDING_DURATION
from src.shared.result import Err, Ok, Result
from src.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_BGE_M3_MODEL = "BAAI/bge-m3"
_BGE_M3_DIMENSION = 1024


class BgeEmbedder(IEmbedder):
    """
    Embedder dùng BGE-M3 via FlagEmbedding.
    Model được load lazy lần đầu tiên gọi embed().
    """

    def __init__(self, config: EmbedderConfig) -> None:
        self._config = config
        self._model_name = config.bge_model or _BGE_M3_MODEL

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return _BGE_M3_DIMENSION

    @cached_property
    def _model(self):
        """
        Load model lần đầu tiên — cached_property đảm bảo chỉ load 1 lần.
        Nặng (~2GB), nên lazy load thay vì load lúc khởi tạo container.
        """
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError:
            raise ImportError(
                "FlagEmbedding not installed. Run: uv add FlagEmbedding"
            )

        logger.info("bge_embedder.loading", model=self._model_name)
        model = BGEM3FlagModel(
            self._model_name,
            use_fp16=self._config.bge_use_fp16,
        )
        logger.info("bge_embedder.loaded", model=self._model_name)
        return model

    def embed(self, texts: list[str]) -> Result[list[list[float]], Exception]:
        if not texts:
            return Ok([])

        with tracer.start_as_current_span("bge_embedder.embed") as span:
            span.set_attribute("batch_size", len(texts))
            span.set_attribute("model", self._model_name)

            t0 = time.perf_counter()
            try:
                # BGE-M3: encode trả về dict với "dense_vecs"
                output = self._model.encode(
                    texts,
                    batch_size=self._config.batch_size,
                    max_length=self._config.max_length,
                    return_dense=True,
                    return_sparse=False,
                    return_colbert_vecs=False,
                )
                vectors: list[list[float]] = output["dense_vecs"].tolist()

                elapsed = time.perf_counter() - t0
                EMBEDDING_DURATION.labels(model=self._model_name).observe(elapsed)

                logger.info(
                    "bge_embedder.embedded",
                    count=len(texts),
                    duration_ms=round(elapsed * 1000, 1),
                    model=self._model_name,
                )
                return Ok(vectors)

            except Exception as e:
                logger.error("bge_embedder.failed", error=str(e))
                return Err(EmbedError("BGE embedder failed", cause=e))

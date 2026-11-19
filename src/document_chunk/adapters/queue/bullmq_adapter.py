"""BullMQAdapter — IJobQueue implementation using python-bullmq over Redis."""
import asyncio
import dataclasses
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from document_chunk.domain.ports.job_queue import (
    DOCUMENT_ENRICHMENT_QUEUE_NAME,
    DOCUMENT_PROCESSING_QUEUE_NAME,
    DocumentJobPayload,
    EnrichmentJobPayload,
    IJobQueue,
)
from document_chunk.infrastructure.config import RedisConfig
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)

_T = TypeVar("_T")

# Job còn lưu lại thì BullMQ coi add() cùng jobId là trùng và âm thầm trả job cũ.
# Lượt có generation (core-api cấp, tăng mỗi lần đưa tài liệu vào hàng đợi) có
# jobId riêng nên giữ job xong/hỏng một thời gian để xem lại cũng không chặn lượt
# sau. Lượt generation = 0 (job cũ, gRPC EnqueueDocument) dùng chung một jobId nên
# phải xoá ngay khi xong/hỏng, nếu không lần xử lý lại sau đó bị gộp vào job cũ.
_RETAIN_COMPLETED = {"age": 24 * 3600, "count": 1000}
_RETAIN_FAILED = {"age": 14 * 24 * 3600}


def build_job_id(document_id: str, processing_generation: int) -> str:
    """
    ``<document_id>-g<generation>``: mỗi lượt xử lý một job riêng.

    Không dùng ':' (BullMQ Node 5 cấm customId chứa ':', core-api cùng đọc queue
    này) và luôn có '-g' nên không bao giờ là số nguyên thuần.
    """
    if not document_id or ":" in document_id:
        raise ValueError(f"document_id không dùng được làm jobId BullMQ: {document_id!r}")
    return f"{document_id}-g{processing_generation}"


def build_job_options(processing_generation: int) -> dict:
    options: dict = {
        "attempts": 3,
        "backoff": {"type": "exponential", "delay": 5000},
    }
    if processing_generation > 0:
        options["removeOnComplete"] = dict(_RETAIN_COMPLETED)
        options["removeOnFail"] = dict(_RETAIN_FAILED)
    else:
        options["removeOnComplete"] = True
        options["removeOnFail"] = True
    return options


def _run_coroutine(coroutine: Coroutine[Any, Any, _T]) -> _T:
    """Run an async BullMQ call from both sync and async-hosted callers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="bullmq-enqueue") as executor:
        return executor.submit(asyncio.run, coroutine).result()


class BullMQAdapter(IJobQueue):
    def __init__(self, config: RedisConfig) -> None:
        self._config = config

    def enqueue(self, payload: DocumentJobPayload) -> Result[str, Exception]:
        try:
            job_id = _run_coroutine(
                self._async_enqueue(
                    queue_name=DOCUMENT_PROCESSING_QUEUE_NAME,
                    job_name=payload.document_id,
                    payload=payload,
                    job_id=build_job_id(payload.document_id, payload.processing_generation),
                )
            )
            logger.info("queue.enqueued", document_id=payload.document_id, job_id=job_id)
            return Ok(job_id)
        except Exception as e:
            logger.error("queue.enqueue_failed", document_id=payload.document_id, error=str(e))
            return Err(e)

    def enqueue_enrichment(self, payload: EnrichmentJobPayload) -> Result[str, Exception]:
        try:
            job_id = _run_coroutine(
                self._async_enqueue(
                    queue_name=DOCUMENT_ENRICHMENT_QUEUE_NAME,
                    job_name=payload.document_id,
                    payload=payload,
                    job_id=build_job_id(payload.document_id, payload.processing_generation),
                )
            )
            logger.info(
                "queue.enrichment_enqueued",
                document_id=payload.document_id,
                job_id=job_id,
            )
            return Ok(job_id)
        except Exception as e:
            logger.error(
                "queue.enrichment_enqueue_failed",
                document_id=payload.document_id,
                error=str(e),
            )
            return Err(e)

    async def _async_enqueue(
        self,
        queue_name: str,
        job_name: str,
        payload: DocumentJobPayload | EnrichmentJobPayload,
        job_id: str,
    ) -> str:
        from bullmq import Queue

        data = dataclasses.asdict(payload)
        opts: dict = {
            "host": self._config.host,
            "port": self._config.port,
            "db": self._config.db,
        }
        if self._config.password:
            opts["password"] = self._config.password

        queue = Queue(queue_name, {"connection": opts})
        try:
            added = await queue.add(
                job_name,
                data,
                # Cùng tài liệu + cùng lượt = cùng job (chặn gửi trùng); lượt mới = job mới.
                {**build_job_options(payload.processing_generation), "jobId": job_id},
            )
            return added.id
        finally:
            await queue.close()

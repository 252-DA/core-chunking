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
                {
                    "jobId": job_name,   # idempotent: same document_id = same job
                    "attempts": 3,
                    "backoff": {"type": "exponential", "delay": 5000},
                },
            )
            return added.id
        finally:
            await queue.close()

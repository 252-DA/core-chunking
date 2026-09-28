from unittest.mock import AsyncMock

import pytest

from document_chunk.adapters.queue.bullmq_adapter import BullMQAdapter
from document_chunk.domain.ports.job_queue import (
    DOCUMENT_ENRICHMENT_QUEUE_NAME,
    EnrichmentJobPayload,
)
from document_chunk.infrastructure.config import RedisConfig


@pytest.mark.asyncio
async def test_enqueue_enrichment_from_running_event_loop(monkeypatch):
    adapter = BullMQAdapter(RedisConfig())
    async_enqueue = AsyncMock(return_value="enrichment-job-001")
    monkeypatch.setattr(adapter, "_async_enqueue", async_enqueue)
    payload = EnrichmentJobPayload(document_id="doc-001")

    result = adapter.enqueue_enrichment(payload)

    assert result.is_ok()
    assert result.unwrap() == "enrichment-job-001"
    async_enqueue.assert_awaited_once_with(
        queue_name=DOCUMENT_ENRICHMENT_QUEUE_NAME,
        job_name="doc-001",
        payload=payload,
    )

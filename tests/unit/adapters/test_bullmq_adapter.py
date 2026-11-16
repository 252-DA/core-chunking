import sys
import types
from unittest.mock import AsyncMock

import pytest

from document_chunk.adapters.queue.bullmq_adapter import (
    BullMQAdapter,
    build_job_id,
    build_job_options,
)
from document_chunk.domain.ports.job_queue import (
    DOCUMENT_ENRICHMENT_QUEUE_NAME,
    DOCUMENT_PROCESSING_QUEUE_NAME,
    DocumentJobPayload,
    EnrichmentJobPayload,
)
from document_chunk.infrastructure.config import RedisConfig


@pytest.mark.asyncio
async def test_enqueue_enrichment_from_running_event_loop(monkeypatch):
    adapter = BullMQAdapter(RedisConfig())
    async_enqueue = AsyncMock(return_value="enrichment-job-001")
    monkeypatch.setattr(adapter, "_async_enqueue", async_enqueue)
    payload = EnrichmentJobPayload(document_id="doc-001", processing_generation=4)

    result = adapter.enqueue_enrichment(payload)

    assert result.is_ok()
    assert result.unwrap() == "enrichment-job-001"
    async_enqueue.assert_awaited_once_with(
        queue_name=DOCUMENT_ENRICHMENT_QUEUE_NAME,
        job_name="doc-001",
        payload=payload,
        job_id="doc-001-g4",
    )


def test_enqueue_document_job_id_follows_generation(monkeypatch):
    adapter = BullMQAdapter(RedisConfig())
    async_enqueue = AsyncMock(return_value="x")
    monkeypatch.setattr(adapter, "_async_enqueue", async_enqueue)
    payload = DocumentJobPayload(
        document_id="doc-001", storage_key="k", file_name="f.pdf", processing_generation=7
    )

    assert adapter.enqueue(payload).is_ok()

    kwargs = async_enqueue.await_args.kwargs
    assert kwargs["queue_name"] == DOCUMENT_PROCESSING_QUEUE_NAME
    assert kwargs["job_id"] == "doc-001-g7"


def test_job_id_differs_per_generation_so_new_run_is_not_merged_into_old_job():
    assert build_job_id("doc-001", 1) != build_job_id("doc-001", 2)
    assert build_job_id("doc-001", 2) == build_job_id("doc-001", 2)


def test_job_id_is_valid_for_bullmq_node():
    job_id = build_job_id("0b5f2f0e-7a7c-4d3a-9d44-7b1b8c8f6a11", 12)

    # BullMQ Node 5 cấm ':' và cấm id là số nguyên thuần.
    assert ":" not in job_id
    assert not job_id.isdigit()
    assert build_job_id("12345", 0) == "12345-g0"
    assert not build_job_id("12345", 0).isdigit()


@pytest.mark.parametrize("bad", ["", "a:b"])
def test_job_id_rejects_unusable_document_id(bad):
    with pytest.raises(ValueError):
        build_job_id(bad, 1)


def test_enqueue_returns_err_instead_of_raising_on_bad_document_id():
    adapter = BullMQAdapter(RedisConfig())

    result = adapter.enqueue_enrichment(EnrichmentJobPayload(document_id="a:b"))

    assert result.is_err()


def test_job_options_remove_jobs_so_same_id_can_be_added_again():
    # generation 0 dùng chung một jobId: job xong/hỏng phải biến mất ngay.
    legacy = build_job_options(0)
    assert legacy["removeOnComplete"] is True
    assert legacy["removeOnFail"] is True

    # generation > 0 có jobId riêng: giữ lại để xem, có trần theo tuổi/số lượng.
    current = build_job_options(3)
    assert current["removeOnComplete"] == {"age": 24 * 3600, "count": 1000}
    assert current["removeOnFail"] == {"age": 14 * 24 * 3600}
    assert current["attempts"] == 3
    assert current["backoff"] == {"type": "exponential", "delay": 5000}


@pytest.mark.asyncio
async def test_async_enqueue_sends_job_id_and_remove_options_to_bullmq(monkeypatch):
    added: list[tuple] = []

    class FakeQueue:
        def __init__(self, name, opts):
            self.name = name

        async def add(self, name, data, opts):
            added.append((self.name, name, data, opts))
            return types.SimpleNamespace(id=opts["jobId"])

        async def close(self):
            pass

    monkeypatch.setitem(sys.modules, "bullmq", types.SimpleNamespace(Queue=FakeQueue))
    adapter = BullMQAdapter(RedisConfig())
    payload = EnrichmentJobPayload(document_id="doc-001", processing_generation=5)

    job_id = await adapter._async_enqueue(
        queue_name=DOCUMENT_ENRICHMENT_QUEUE_NAME,
        job_name="doc-001",
        payload=payload,
        job_id=build_job_id("doc-001", 5),
    )

    assert job_id == "doc-001-g5"
    queue_name, name, data, opts = added[0]
    assert queue_name == DOCUMENT_ENRICHMENT_QUEUE_NAME
    assert name == "doc-001"
    assert data == {"document_id": "doc-001", "processing_generation": 5}
    assert opts["jobId"] == "doc-001-g5"
    assert "removeOnComplete" in opts and "removeOnFail" in opts

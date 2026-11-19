"""IJobQueue — port for async document processing job queue."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from document_chunk.shared.result import Result

# Queue names — single source of truth
DOCUMENT_PROCESSING_QUEUE_NAME = "document_processing"
DOCUMENT_ENRICHMENT_QUEUE_NAME = "document_enrichment"
CONTENT_GENERATION_QUEUE_NAME = "content_generation"
OUTBOX_RELAY_QUEUE_NAME = "outbox_relay"
ANSWER_GRADING_QUEUE_NAME = "answer_grading"


@dataclass
class DocumentJobPayload:
    document_id: str
    storage_key: str
    file_name: str
    language: str | None = None
    metadata: dict = field(default_factory=dict)
    # Lượt xử lý do core-api cấp (documents.processing_generation) mỗi lần đưa
    # tài liệu vào hàng đợi; worker gửi kèm mọi kết quả trạng thái để core-api
    # bỏ kết quả của lượt cũ. 0 = job cũ chưa có trường này.
    processing_generation: int = 0

    def __post_init__(self) -> None:
        _check_generation(self.processing_generation)


@dataclass
class EnrichmentJobPayload:
    document_id: str
    # Chuyển tiếp từ job xử lý tài liệu đã sinh ra lần enrichment này.
    processing_generation: int = 0

    def __post_init__(self) -> None:
        _check_generation(self.processing_generation)


def _check_generation(value: object) -> None:
    # Runner đổi ValueError thành UnrecoverableError: job sai dạng không retry.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"processing_generation must be an integer >= 0, got {value!r}")


class IJobQueue(ABC):
    @abstractmethod
    def enqueue(self, payload: DocumentJobPayload) -> Result[str, Exception]:
        """Enqueue a document processing job. Returns job_id on success."""
        ...

    @abstractmethod
    def enqueue_enrichment(self, payload: EnrichmentJobPayload) -> Result[str, Exception]:
        """Enqueue a document enrichment job. Returns job_id on success."""
        ...

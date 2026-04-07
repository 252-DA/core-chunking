"""IJobQueue — port for async document processing job queue."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from src.shared.result import Result

# Queue names — single source of truth
DOCUMENT_PROCESSING_QUEUE_NAME = "document_processing"
DOCUMENT_ENRICHMENT_QUEUE_NAME = "document_enrichment"


@dataclass
class DocumentJobPayload:
    document_id: str
    storage_key: str
    file_name: str
    language: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class EnrichmentJobPayload:
    document_id: str


class IJobQueue(ABC):
    @abstractmethod
    def enqueue(self, payload: DocumentJobPayload) -> Result[str, Exception]:
        """Enqueue a document processing job. Returns job_id on success."""
        ...

    @abstractmethod
    def enqueue_enrichment(self, payload: EnrichmentJobPayload) -> Result[str, Exception]:
        """Enqueue a document enrichment job. Returns job_id on success."""
        ...

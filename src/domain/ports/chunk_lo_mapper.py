from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.domain.entities.curriculum import Curriculum
from src.domain.ports.metadata_store import StoredChunkMetadata
from src.shared.result import Result


@dataclass(frozen=True)
class ChunkLOMapping:
    chunk_id: str
    lo_id: str
    confidence: float
    source: str  # "heading" | "embedding" | "llm" | comma-joined combo


class IChunkLOMapper(ABC):
    @abstractmethod
    def map(
        self,
        chunks: list[StoredChunkMetadata],
        curriculum: Curriculum,
    ) -> Result[list[ChunkLOMapping], Exception]:
        ...

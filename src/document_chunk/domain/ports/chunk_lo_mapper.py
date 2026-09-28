from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from document_chunk.domain.entities.curriculum import Curriculum
from document_chunk.domain.ports.metadata_store import StoredChunkMetadata
from document_chunk.shared.result import Result


@dataclass(frozen=True)
class ChunkLOMapping:
    chunk_id: str
    lo_id: str
    confidence: float
    source: str  # "heading" | "embedding" | "llm" | comma-joined combo


@dataclass(frozen=True)
class MappingHints:
    """Vị trí của tài liệu trong học phần, xác định trước khi map từng chunk.

    - ``document_chapter``: chương của cả tài liệu (bài giảng/bài tập) — từ tên
      module, tên file, khớp nội dung hoặc giảng viên chọn.
    - ``chunk_chapters``: chương theo từng chunk, cho tài liệu tham khảo trải
      nhiều chương (khớp từng phần của sách với đề cương).
    """
    role: str = "lecture"
    document_chapter: str | None = None
    chunk_chapters: dict[str, str] = field(default_factory=dict)


class IChunkLOMapper(ABC):
    @abstractmethod
    def map(
        self,
        chunks: list[StoredChunkMetadata],
        curriculum: Curriculum,
        hints: MappingHints | None = None,
    ) -> Result[list[ChunkLOMapping], Exception]:
        ...

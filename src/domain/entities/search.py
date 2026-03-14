from dataclasses import dataclass, field
from datetime import datetime

from src.domain.entities.chunk import Chunk
from src.domain.entities.document import DocumentType


@dataclass(frozen=True)
class SearchFilter:
    """
    Optional filters áp dụng trước khi search vector.
    Dùng để narrow down search space — filter ở metadata DB (sau này)
    hoặc ở Qdrant payload filter (trước mắt).
    """
    doc_types: tuple[DocumentType, ...] = ()     # [] = không filter
    document_ids: tuple[str, ...] = ()           # filter theo document cụ thể
    language: str | None = None                  # "vi", "en", None = all
    uploaded_after: datetime | None = None
    uploaded_before: datetime | None = None
    heading_path_contains: str | None = None     # tìm trong heading path


@dataclass(frozen=True)
class SearchQuery:
    """
    Input của search use case.
    """
    text: str                                    # câu query của user
    top_k: int = 10                              # số kết quả trả về
    score_threshold: float = 0.0                 # loại bỏ kết quả dưới ngưỡng này
    filters: SearchFilter = field(default_factory=SearchFilter)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Search query text cannot be empty")
        if self.top_k < 1:
            raise ValueError("top_k must be at least 1")
        if not (0.0 <= self.score_threshold <= 1.0):
            raise ValueError("score_threshold must be between 0.0 and 1.0")


@dataclass(frozen=True)
class SearchResult:
    """
    Một kết quả search — chunk kèm relevance score.
    """
    chunk: Chunk
    score: float             # cosine similarity score [0.0, 1.0]
    rank: int                # thứ hạng trong kết quả (1-based)


@dataclass(frozen=True)
class SearchResponse:
    """
    Output của search use case — danh sách kết quả + metadata.
    """
    query: SearchQuery
    results: tuple[SearchResult, ...]
    total_found: int
    search_duration_ms: float

    @property
    def has_results(self) -> bool:
        return len(self.results) > 0

    @property
    def top_result(self) -> SearchResult | None:
        return self.results[0] if self.results else None

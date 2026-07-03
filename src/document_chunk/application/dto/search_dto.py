from pydantic import BaseModel, Field, field_validator

from document_chunk.domain.entities.document import DocumentType


class SearchRequest(BaseModel):
    """
    Input của SearchChunksUseCase.
    """
    query: str
    top_k: int = Field(default=10, ge=1, le=100)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)

    # Filters — tất cả optional
    doc_types: list[DocumentType] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    language: str | None = None
    course_id: str | None = None
    owner_id: str | None = None

    @field_validator("query")
    @classmethod
    def query_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Query must not be empty")
        return v.strip()


class SearchResultItem(BaseModel):
    """Một kết quả search."""
    chunk_id: str
    document_id: str
    document_name: str
    doc_type: DocumentType
    heading_path: list[str]
    content: str
    score: float
    rank: int
    page_number: int | None


class SearchResponse(BaseModel):
    """
    Output của SearchChunksUseCase.
    """
    query: str
    results: list[SearchResultItem]
    total_found: int
    search_time_ms: float

    @property
    def has_results(self) -> bool:
        return len(self.results) > 0

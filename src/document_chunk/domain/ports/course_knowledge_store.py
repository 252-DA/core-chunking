"""
Cổng đọc/ghi tri thức theo khóa học cho chatbot: tài liệu, trang, đoạn láng
giềng và đồ thị quan hệ giữa các ý.

Tách khỏi ``IMetadataStore`` vì hai bên dùng nó (mcp-service đọc, worker ghi)
và vì nó nhỏ, có phạm vi rõ: chatbot không cần cả bề mặt của metadata store.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from document_chunk.shared.result import Result


@dataclass(frozen=True)
class CourseDocumentInfo:
    document_id: str
    title: str
    role: str
    chapter_code: str | None
    page_count: int
    chunk_count: int


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    sort_order: int
    heading_path: tuple[str, ...]
    page_number: int | None
    content: str


@dataclass(frozen=True)
class ConceptRelation:
    relation_id: str
    source_label: str
    relation_type: str
    target_label: str
    evidence_quote: str
    chunk_id: str
    document_id: str
    page_number: int | None
    hop: int = 1


@dataclass(frozen=True)
class NewRelation:
    """Một quan hệ vừa trích, đã qua kiểm tra trích dẫn, chờ ghi."""
    chunk_id: str
    document_id: str
    source_label: str
    source_key: str
    relation_type: str
    target_label: str
    target_key: str
    evidence_quote: str
    page_number: int | None


class ICourseKnowledgeStore(ABC):
    # ---- đọc: dùng cho công cụ MCP ----
    @abstractmethod
    def list_course_documents(
        self, course_id: str, chapter_code: str | None = None
    ) -> Result[list[CourseDocumentInfo], Exception]: ...

    @abstractmethod
    def list_hidden_document_ids(
        self, course_id: str | None = None
    ) -> Result[list[str], Exception]:
        """
        Id các tài liệu học viên không được thấy của khóa học (hoặc mọi khóa nếu None): ẩn ở
        LMS (``lms_published = false``) hoặc đã xoá (``deleted_at``), kể cả khi vector của
        tài liệu xoá chưa được dọn khỏi Qdrant.
        """

    @abstractmethod
    def list_page_chunks(
        self, document_id: str, page_number: int, course_id: str | None = None
    ) -> Result[list[KnowledgeChunk], Exception]: ...

    @abstractmethod
    def get_neighbor_chunks(
        self, chunk_id: str, window: int
    ) -> Result[list[KnowledgeChunk], Exception]:
        """Đoạn ``chunk_id`` cùng tối đa ``window`` đoạn trước và sau, theo thứ tự tài liệu."""

    @abstractmethod
    def find_relations(
        self, course_id: str, concept_key: str, hops: int, limit: int
    ) -> Result[list[ConceptRelation], Exception]:
        """Cạnh quanh các nút khớp ``concept_key``, tối đa ``hops`` bước."""

    # ---- ghi: dùng cho worker trích quan hệ ----
    @abstractmethod
    def list_document_chunks(
        self, document_id: str
    ) -> Result[list[KnowledgeChunk], Exception]: ...

    @abstractmethod
    def replace_relations(
        self,
        *,
        course_id: str,
        chunk_ids: list[str],
        extraction_version: str,
        model: str | None,
        relations: list[NewRelation],
    ) -> Result[int, Exception]:
        """Thay quan hệ của ``chunk_ids`` bằng ``relations``; trả số dòng đã ghi."""


__all__ = [
    "ConceptRelation",
    "CourseDocumentInfo",
    "ICourseKnowledgeStore",
    "KnowledgeChunk",
    "NewRelation",
]

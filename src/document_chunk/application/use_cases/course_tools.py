"""
Công cụ tra cứu cho chatbot: tra theo trang, liệt kê tài liệu, đề cương, đoạn
láng giềng và quan hệ giữa các ý.

Mỗi hàm trả một dict nhỏ, có giới hạn kích thước, để mô hình đọc được trong một
lượt mà không tràn ngữ cảnh. Delivery (MCP) chỉ việc gọi và trả lại.
"""
from __future__ import annotations

from document_chunk.domain.concept_keys import concept_key
from document_chunk.domain.exceptions import ProcessingError
from document_chunk.domain.ports.course_knowledge_store import (
    ConceptRelation,
    ICourseKnowledgeStore,
    KnowledgeChunk,
)
from document_chunk.domain.ports.metadata_store import IMetadataStore
from document_chunk.shared.result import Err, Ok, Result

MAX_CHUNK_CHARS = 1800
MAX_PAGE_CHUNKS = 8
MAX_NEIGHBOR_WINDOW = 3
MAX_DOCUMENTS = 60
MAX_RELATIONS = 25
MAX_HOPS = 2
MAX_OUTLINE_LOS = 60


def _chunk_json(chunk: KnowledgeChunk) -> dict:
    text = chunk.content.strip()
    return {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "page_number": chunk.page_number,
        "heading_path": list(chunk.heading_path),
        "content": text[:MAX_CHUNK_CHARS],
        "truncated": len(text) > MAX_CHUNK_CHARS,
    }


def _relation_json(rel: ConceptRelation) -> dict:
    return {
        "source": rel.source_label,
        "relation": rel.relation_type,
        "target": rel.target_label,
        "evidence_quote": rel.evidence_quote,
        "chunk_id": rel.chunk_id,
        "document_id": rel.document_id,
        "page_number": rel.page_number,
        "hop": rel.hop,
    }


class CourseToolsUseCase:
    def __init__(
        self,
        knowledge_store: ICourseKnowledgeStore,
        metadata_store: IMetadataStore,
    ) -> None:
        self._knowledge = knowledge_store
        self._metadata = metadata_store

    def get_document_page(self, document_id: str, page: int) -> Result[dict, Exception]:
        if page < 1:
            return Err(ProcessingError("page must be at least 1"))
        result = self._knowledge.list_page_chunks(document_id, page)
        if result.is_err():
            return Err(result.error)
        chunks = result.unwrap()
        return Ok(
            {
                "document_id": document_id,
                "page": page,
                "chunks": [_chunk_json(c) for c in chunks[:MAX_PAGE_CHUNKS]],
                "total_chunks": len(chunks),
            }
        )

    def list_documents(
        self, course_id: str, chapter_code: str | None = None
    ) -> Result[dict, Exception]:
        result = self._knowledge.list_course_documents(course_id, chapter_code or None)
        if result.is_err():
            return Err(result.error)
        docs = result.unwrap()
        return Ok(
            {
                "course_id": course_id,
                "chapter_code": chapter_code,
                "documents": [
                    {
                        "document_id": d.document_id,
                        "title": d.title,
                        "role": d.role,
                        "chapter_code": d.chapter_code,
                        "pages": d.page_count,
                    }
                    for d in docs[:MAX_DOCUMENTS]
                ],
                "total_documents": len(docs),
            }
        )

    def get_outline(
        self, course_id: str, chapter_code: str | None = None
    ) -> Result[dict, Exception]:
        curriculum = self._metadata.get_curriculum(course_id)
        if curriculum.is_err():
            return Err(curriculum.error)
        data = curriculum.unwrap()
        if data is None:
            return Err(ProcessingError(f"No curriculum for course {course_id}"))
        course, chapters, los, assessments = data
        links_result = self._metadata.list_chapter_lo_links(course_id)
        links = links_result.unwrap() if links_result.is_ok() else []

        wanted = (chapter_code or "").strip().casefold()
        lo_by_id = {lo.lo_id: lo for lo in los}
        out_chapters = []
        for chapter in chapters:
            if wanted and chapter.code.casefold() != wanted:
                continue
            chapter_los = [
                lo_by_id[link.lo_id].code
                for link in links
                if link.chapter_id == chapter.chapter_id and link.lo_id in lo_by_id
            ]
            out_chapters.append(
                {
                    "code": chapter.code,
                    "title": chapter.title,
                    "topics": chapter.topics,
                    "los": chapter_los,
                }
            )
        wanted_codes = {code for ch in out_chapters for code in ch["los"]}
        out_los = [
            {
                "code": lo.code,
                "statement": lo.statement_vi,
                "bloom_level": lo.bloom_level,
            }
            for lo in los
            if not wanted or lo.code in wanted_codes
        ][:MAX_OUTLINE_LOS]
        return Ok(
            {
                "course": {"code": course.code, "title": course.title_vi},
                "chapters": out_chapters,
                "learning_outcomes": out_los,
                "assessments": [
                    {"code": a.code, "name": a.name_vi, "category": a.category, "weight": a.weight}
                    for a in assessments
                ],
            }
        )

    def get_neighbors(self, chunk_id: str, window: int = 1) -> Result[dict, Exception]:
        window = max(1, min(int(window), MAX_NEIGHBOR_WINDOW))
        result = self._knowledge.get_neighbor_chunks(chunk_id, window)
        if result.is_err():
            return Err(result.error)
        chunks = result.unwrap()
        return Ok(
            {
                "anchor_chunk_id": chunk_id,
                "window": window,
                "chunks": [_chunk_json(c) for c in chunks],
            }
        )

    def get_concept_relations(
        self,
        course_id: str,
        concept: str,
        hops: int = 1,
        limit: int = 15,
    ) -> Result[dict, Exception]:
        key = concept_key(concept)
        if not key:
            return Err(ProcessingError("concept must not be empty"))
        hops = max(1, min(int(hops), MAX_HOPS))
        limit = max(1, min(int(limit), MAX_RELATIONS))
        result = self._knowledge.find_relations(course_id, key, hops, limit)
        if result.is_err():
            return Err(result.error)
        relations = result.unwrap()
        return Ok(
            {
                "concept": concept,
                "matched_key": key,
                "hops": hops,
                "relations": [_relation_json(r) for r in relations],
                "found": len(relations),
            }
        )


__all__ = ["CourseToolsUseCase"]

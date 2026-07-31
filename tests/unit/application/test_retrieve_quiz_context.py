from unittest.mock import MagicMock

from document_chunk.application.dto.search_dto import (
    SearchResponse,
    SearchResultItem,
)
from document_chunk.application.use_cases.retrieve_quiz_context import (
    RetrieveQuizContextRequest,
    RetrieveQuizContextUseCase,
)
from document_chunk.domain.entities.document import DocumentType
from document_chunk.domain.ports.metadata_store import (
    StoredChunkMetadata,
    StoredCourse,
    StoredLearningOutcome,
)
from document_chunk.shared.result import Ok


def _curriculum():
    return (
        StoredCourse(
            course_id="CO3115",
            code="CO3115",
            title_vi="Phân tích và Thiết kế Hệ thống",
            extraction_confidence=0.9,
        ),
        [],
        [
            StoredLearningOutcome(
                lo_id="CO3115:L.O.3.1",
                course_id="CO3115",
                code="L.O.3.1",
                parent_code="L.O.3",
                statement_vi="Phân tích yêu cầu chức năng",
                statement_en="Analyze functional requirements",
                bloom_level="analyze",
            )
        ],
        [],
    )


def _chunk(chunk_id: str, content: str, chunk_index: int) -> StoredChunkMetadata:
    return StoredChunkMetadata(
        chunk_id=chunk_id,
        document_id="doc-001",
        chunk_index=chunk_index,
        heading_path=("Chương 3",),
        heading_level=1,
        page_number=chunk_index + 1,
        content_length=len(content),
        content_text=content,
    )


def test_retrieves_bounded_lo_context():
    store = MagicMock()
    store.get_curriculum.return_value = Ok(_curriculum())
    store.list_chunks_for_lo.return_value = Ok(
        [
            _chunk("chunk-1", "A" * 100, 0),
            _chunk("chunk-2", "B" * 100, 1),
        ]
    )
    use_case = RetrieveQuizContextUseCase(
        metadata_store=store,
        max_chunk_chars=60,
        max_total_chars=120,
    )

    result = use_case.execute(
        RetrieveQuizContextRequest(
            course_id="CO3115",
            lo_code="3.1",
            top_k=2,
        )
    )

    assert result.is_ok()
    response = result.unwrap()
    assert response.lo_id == "CO3115:L.O.3.1"
    assert response.bloom_level == "analyze"
    assert response.total_chars == 120
    assert [item.chunk_id for item in response.chunks] == ["chunk-1", "chunk-2"]
    assert all(len(item.content) == 60 for item in response.chunks)
    store.list_chunks_for_lo.assert_called_once_with("CO3115:L.O.3.1")


def test_semantic_search_reranks_only_chunks_mapped_to_lo():
    mapped_a = _chunk("chunk-a", "Context A", 0)
    mapped_b = _chunk("chunk-b", "Context B", 1)
    store = MagicMock()
    store.get_curriculum.return_value = Ok(_curriculum())
    store.list_chunks_for_lo.return_value = Ok([mapped_a, mapped_b])

    semantic_search = MagicMock()
    semantic_search.execute.return_value = Ok(
        SearchResponse(
            query="functional requirements",
            results=[
                SearchResultItem(
                    chunk_id="unrelated",
                    document_id="doc-002",
                    document_name="other.pdf",
                    doc_type=DocumentType.PDF,
                    heading_path=[],
                    content="Unrelated course context",
                    score=0.99,
                    rank=1,
                    page_number=1,
                ),
                SearchResultItem(
                    chunk_id="chunk-b",
                    document_id="doc-001",
                    document_name="course.pdf",
                    doc_type=DocumentType.PDF,
                    heading_path=["Chương 3"],
                    content="Context B",
                    score=0.91,
                    rank=2,
                    page_number=2,
                ),
            ],
            total_found=2,
            search_time_ms=2.5,
        )
    )
    use_case = RetrieveQuizContextUseCase(
        metadata_store=store,
        semantic_search=semantic_search,
    )

    result = use_case.execute(
        RetrieveQuizContextRequest(
            course_id="CO3115",
            lo_code="L.O.3.1",
            query="functional requirements",
            top_k=2,
        )
    )

    assert result.is_ok()
    chunks = result.unwrap().chunks
    assert [item.chunk_id for item in chunks] == ["chunk-b", "chunk-a"]
    assert chunks[0].score == 0.91
    assert all(item.chunk_id != "unrelated" for item in chunks)


def test_unknown_learning_outcome_returns_error():
    store = MagicMock()
    store.get_curriculum.return_value = Ok(_curriculum())
    use_case = RetrieveQuizContextUseCase(metadata_store=store)

    result = use_case.execute(
        RetrieveQuizContextRequest(
            course_id="CO3115",
            lo_code="L.O.99",
        )
    )

    assert result.is_err()
    store.list_chunks_for_lo.assert_not_called()

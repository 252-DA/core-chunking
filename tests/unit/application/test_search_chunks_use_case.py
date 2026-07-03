from document_chunk.application.dto.search_dto import SearchRequest
from document_chunk.application.use_cases.search_chunks import SearchChunksUseCase
from document_chunk.domain.entities.document import DocumentType


class TestSearchChunksUseCase:
    def test_propagates_all_supported_filters_to_vector_store(
        self,
        mock_embedder,
        mock_vector_store,
    ):
        use_case = SearchChunksUseCase(
            embedder=mock_embedder,
            vector_store=mock_vector_store,
        )

        result = use_case.execute(
            SearchRequest(
                query="calculus",
                top_k=5,
                score_threshold=0.2,
                doc_types=[DocumentType.PDF],
                document_ids=["doc-001"],
                language="en",
                course_id="course-001",
                owner_id="owner-001",
            )
        )

        assert result.is_ok()
        filters = mock_vector_store.search.call_args.kwargs["filters"]
        assert filters.doc_types == (DocumentType.PDF,)
        assert filters.document_ids == ("doc-001",)
        assert filters.language == "en"
        assert filters.course_id == "course-001"
        assert filters.owner_id == "owner-001"

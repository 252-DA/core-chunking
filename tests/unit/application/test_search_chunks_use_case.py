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


class TestExcludeUnpublished:
    """Chatbot (học viên) không được thấy tài liệu bị ẩn ở LMS; giảng viên thì vẫn thấy."""

    @staticmethod
    def _store(hidden):
        from unittest.mock import MagicMock

        from document_chunk.shared.result import Ok

        store = MagicMock()
        store.list_unpublished_document_ids.return_value = Ok(hidden)
        return store

    def test_default_request_never_asks_for_hidden_documents(self, mock_embedder, mock_vector_store):
        store = self._store(["hidden-1"])
        use_case = SearchChunksUseCase(mock_embedder, mock_vector_store, knowledge_store=store)

        assert use_case.execute(SearchRequest(query="q", course_id="c1")).is_ok()

        store.list_unpublished_document_ids.assert_not_called()
        filters = mock_vector_store.search.call_args.kwargs["filters"]
        assert filters.exclude_document_ids == ()

    def test_flag_turns_hidden_ids_into_a_vector_store_exclusion(self, mock_embedder, mock_vector_store):
        store = self._store(["hidden-1", "hidden-2"])
        use_case = SearchChunksUseCase(mock_embedder, mock_vector_store, knowledge_store=store)

        result = use_case.execute(
            SearchRequest(query="q", course_id="CO3137", top_k=7, exclude_unpublished=True)
        )

        assert result.is_ok()
        store.list_unpublished_document_ids.assert_called_once_with("CO3137")
        kwargs = mock_vector_store.search.call_args.kwargs
        assert kwargs["filters"].exclude_document_ids == ("hidden-1", "hidden-2")
        assert kwargs["top_k"] == 7  # loại ở Qdrant nên không cần lấy dư

    def test_no_hidden_documents_adds_no_exclusion(self, mock_embedder, mock_vector_store):
        use_case = SearchChunksUseCase(mock_embedder, mock_vector_store, knowledge_store=self._store([]))

        assert use_case.execute(
            SearchRequest(query="q", course_id="c1", exclude_unpublished=True)
        ).is_ok()

        assert mock_vector_store.search.call_args.kwargs["filters"].exclude_document_ids == ()

    def test_hidden_lookup_failure_refuses_instead_of_leaking(self, mock_embedder, mock_vector_store):
        from document_chunk.shared.result import Err

        store = self._store([])
        store.list_unpublished_document_ids.return_value = Err(RuntimeError("db down"))
        use_case = SearchChunksUseCase(mock_embedder, mock_vector_store, knowledge_store=store)

        result = use_case.execute(
            SearchRequest(query="q", course_id="c1", exclude_unpublished=True)
        )

        assert result.is_err()
        mock_vector_store.search.assert_not_called()

    def test_flag_without_a_knowledge_store_refuses(self, mock_embedder, mock_vector_store):
        use_case = SearchChunksUseCase(mock_embedder, mock_vector_store)

        result = use_case.execute(
            SearchRequest(query="q", course_id="c1", exclude_unpublished=True)
        )

        assert result.is_err()
        mock_vector_store.search.assert_not_called()

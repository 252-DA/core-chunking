from document_chunk.application.use_cases.get_cards import GetCardsRequest, GetCardsUseCase
from document_chunk.application.use_cases.get_document_status import (
    GetDocumentStatusRequest,
    GetDocumentStatusUseCase,
)
from document_chunk.application.use_cases.get_quiz import GetQuizRequest, GetQuizUseCase
from document_chunk.domain.ports.metadata_store import IngestionStatus, StoredLessonCard, StoredQuizItem
from document_chunk.shared.result import Ok


class TestGeneratedContentUseCases:
    def test_get_cards_groups_cards_by_heading_path(self, sample_document, mock_metadata_store):
        mock_metadata_store.get.return_value = Ok(sample_document)
        mock_metadata_store.list_lesson_cards.return_value = Ok(
            [
                StoredLessonCard(
                    card_id="card-001",
                    document_id=sample_document.id,
                    primary_chunk_id="chunk-001",
                    source_chunk_ids=("chunk-001",),
                    heading_path=("Chapter 1", "Matrices"),
                    title="Matrix basics",
                    bullets=("Rectangular arrays",),
                    key_insight="Matrices capture structure.",
                    card_index=0,
                    model_id="gemini-test",
                ),
                StoredLessonCard(
                    card_id="card-002",
                    document_id=sample_document.id,
                    primary_chunk_id="chunk-001",
                    source_chunk_ids=("chunk-001",),
                    heading_path=("Chapter 1", "Matrices"),
                    title="Matrix notation",
                    bullets=("Use rows and columns",),
                    key_insight="Notation clarifies dimensions.",
                    card_index=1,
                    model_id="gemini-test",
                ),
                StoredLessonCard(
                    card_id="card-003",
                    document_id=sample_document.id,
                    primary_chunk_id="chunk-010",
                    source_chunk_ids=("chunk-010",),
                    heading_path=("Chapter 1", "Determinants"),
                    title="Determinants",
                    bullets=("Measure scaling",),
                    key_insight="Determinants summarize invertibility.",
                    card_index=0,
                    model_id="gemini-test",
                ),
            ]
        )

        use_case = GetCardsUseCase(metadata_store=mock_metadata_store)
        result = use_case.execute(GetCardsRequest(document_id=sample_document.id))

        assert result.is_ok()
        response = result.unwrap()
        assert response is not None
        assert response.total_cards == 3
        assert len(response.sections) == 2
        assert response.sections[0].heading_path == ["Chapter 1", "Matrices"]
        assert len(response.sections[0].cards) == 2
        assert response.sections[1].heading_path == ["Chapter 1", "Determinants"]

    def test_get_quiz_returns_none_when_document_is_missing(self, mock_metadata_store):
        mock_metadata_store.get.return_value = Ok(None)

        use_case = GetQuizUseCase(metadata_store=mock_metadata_store)
        result = use_case.execute(GetQuizRequest(document_id="missing-doc"))

        assert result.is_ok()
        assert result.unwrap() is None

    def test_get_document_status_maps_metadata_tuple(self, mock_metadata_store):
        mock_metadata_store.get_document_status.return_value = Ok(
            (IngestionStatus.ENRICHED, None, "pdf/doc-001/test.pdf")
        )

        use_case = GetDocumentStatusUseCase(metadata_store=mock_metadata_store)
        result = use_case.execute(GetDocumentStatusRequest(document_id="doc-001"))

        assert result.is_ok()
        response = result.unwrap()
        assert response is not None
        assert response.status == IngestionStatus.ENRICHED
        assert response.storage_key == "pdf/doc-001/test.pdf"

    def test_get_quiz_maps_questions(self, sample_document, mock_metadata_store):
        mock_metadata_store.get.return_value = Ok(sample_document)
        mock_metadata_store.list_quiz_items.return_value = Ok(
            [
                StoredQuizItem(
                    question_id="quiz-001",
                    document_id=sample_document.id,
                    primary_chunk_id="chunk-001",
                    source_chunk_ids=("chunk-001",),
                    heading_path=("Chapter 1", "Matrices"),
                    question="What is a matrix?",
                    choices=("Array", "Graph", "Scalar", "Function"),
                    correct_index=0,
                    explanation="A matrix is an array.",
                    difficulty="easy",
                    question_index=0,
                    model_id="gemini-test",
                )
            ]
        )

        use_case = GetQuizUseCase(metadata_store=mock_metadata_store)
        result = use_case.execute(GetQuizRequest(document_id=sample_document.id))

        assert result.is_ok()
        response = result.unwrap()
        assert response is not None
        assert response.total_questions == 1
        assert response.questions[0].chunk_id == "chunk-001"

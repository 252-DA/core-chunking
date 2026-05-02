from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.application.dto.generation_dto import (
    CardSection,
    CardsResponse,
    DocumentStatusResponse,
    LessonCardItem,
    QuizQuestionItem,
    QuizResponse,
)
from src.delivery.http import api
from src.domain.ports.metadata_store import IngestionStatus
from src.shared.result import Ok


class _FakeContainer:
    def __init__(self) -> None:
        self.process_document_use_case = MagicMock()
        self.search_chunks_use_case = MagicMock()
        self.delete_document_use_case = MagicMock()
        self.get_document_status_use_case = MagicMock()
        self.get_cards_use_case = MagicMock()
        self.get_quiz_use_case = MagicMock()

    def close(self) -> None:
        return None


class TestHttpApi:
    def test_get_document_status_returns_404_when_missing(self):
        container = _FakeContainer()
        container.get_document_status_use_case.execute.return_value = Ok(None)
        api.app.state.container = container

        client = TestClient(api.app)
        response = client.get("/documents/missing-doc/status")

        assert response.status_code == 404
        assert response.json()["detail"] == "document not found: missing-doc"

    def test_get_cards_returns_grouped_sections(self):
        container = _FakeContainer()
        container.get_cards_use_case.execute.return_value = Ok(
            CardsResponse(
                document_id="doc-001",
                sections=[
                    CardSection(
                        heading_path=["Chapter 1", "Matrices"],
                        cards=[
                            LessonCardItem(
                                card_id="card-001",
                                chunk_id="chunk-001",
                                heading_path=["Chapter 1", "Matrices"],
                                title="Matrices",
                                bullets=["Rectangular arrays"],
                                key_insight="Matrices structure data.",
                                card_index=0,
                            )
                        ],
                    )
                ],
                total_cards=1,
            )
        )
        api.app.state.container = container

        client = TestClient(api.app)
        response = client.get("/documents/doc-001/cards")

        assert response.status_code == 200
        payload = response.json()
        assert payload["document_id"] == "doc-001"
        assert payload["total_cards"] == 1
        assert payload["sections"][0]["cards"][0]["card_id"] == "card-001"

    def test_get_quiz_returns_questions(self):
        container = _FakeContainer()
        container.get_quiz_use_case.execute.return_value = Ok(
            QuizResponse(
                document_id="doc-001",
                questions=[
                    QuizQuestionItem(
                        question_id="quiz-001",
                        chunk_id="chunk-001",
                        question="What is a matrix?",
                        choices=["Array", "Graph", "Scalar", "Function"],
                        correct_index=0,
                        explanation="A matrix is an array.",
                        difficulty="easy",
                    )
                ],
                total_questions=1,
            )
        )
        api.app.state.container = container

        client = TestClient(api.app)
        response = client.get("/documents/doc-001/quiz")

        assert response.status_code == 200
        payload = response.json()
        assert payload["document_id"] == "doc-001"
        assert payload["total_questions"] == 1
        assert payload["questions"][0]["question_id"] == "quiz-001"

    def test_get_document_status_returns_payload(self):
        container = _FakeContainer()
        container.get_document_status_use_case.execute.return_value = Ok(
            DocumentStatusResponse(
                document_id="doc-001",
                status=IngestionStatus.ENRICHED,
                storage_key="pdf/doc-001/test.pdf",
            )
        )
        api.app.state.container = container

        client = TestClient(api.app)
        response = client.get("/documents/doc-001/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["document_id"] == "doc-001"
        assert payload["status"] == "ENRICHED"

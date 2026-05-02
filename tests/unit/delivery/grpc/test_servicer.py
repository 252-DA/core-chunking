from unittest.mock import MagicMock

from src.application.dto.generation_dto import (
    CardSection,
    CardsResponse,
    DocumentStatusResponse,
    LessonCardItem,
    QuizQuestionItem,
    QuizResponse,
)
from src.application.dto.search_dto import SearchResponse
from src.application.use_cases.delete_document import DeleteDocumentResponse
from src.delivery.grpc.proto import chunking_pb2
from src.delivery.grpc.servicer import ChunkingServicer
from src.domain.ports.metadata_store import IngestionStatus
from src.shared.result import Ok


class _FakeContext:
    def abort(self, code, message):
        raise AssertionError(f"unexpected grpc abort: {code} {message}")


class TestChunkingServicer:
    def _build_servicer(self):
        process_use_case = MagicMock()
        search_use_case = MagicMock()
        enqueue_use_case = MagicMock()
        delete_use_case = MagicMock()
        get_document_status_use_case = MagicMock()
        get_cards_use_case = MagicMock()
        get_quiz_use_case = MagicMock()
        servicer = ChunkingServicer(
            process_use_case=process_use_case,
            search_use_case=search_use_case,
            enqueue_use_case=enqueue_use_case,
            delete_use_case=delete_use_case,
            get_document_status_use_case=get_document_status_use_case,
            get_cards_use_case=get_cards_use_case,
            get_quiz_use_case=get_quiz_use_case,
        )
        return (
            servicer,
            process_use_case,
            search_use_case,
            enqueue_use_case,
            delete_use_case,
            get_document_status_use_case,
            get_cards_use_case,
            get_quiz_use_case,
        )

    def test_search_maps_course_and_owner_filters(self):
        (
            servicer,
            _process_use_case,
            search_use_case,
            _enqueue_use_case,
            _delete_use_case,
            _status_use_case,
            _cards_use_case,
            _quiz_use_case,
        ) = self._build_servicer()
        search_use_case.execute.return_value = Ok(
            SearchResponse(
                query="calculus",
                results=[],
                total_found=0,
                search_time_ms=1.23,
            )
        )

        response = servicer.Search(
            chunking_pb2.SearchRequest(
                query="calculus",
                top_k=5,
                course_id="course-001",
                owner_id="owner-001",
            ),
            _FakeContext(),
        )

        dto = search_use_case.execute.call_args.args[0]
        assert dto.course_id == "course-001"
        assert dto.owner_id == "owner-001"
        assert response.query == "calculus"
        assert response.total_found == 0

    def test_get_document_status_includes_storage_key(self):
        (
            servicer,
            _process_use_case,
            _search_use_case,
            _enqueue_use_case,
            _delete_use_case,
            status_use_case,
            _cards_use_case,
            _quiz_use_case,
        ) = self._build_servicer()
        status_use_case.execute.return_value = Ok(
            DocumentStatusResponse(
                document_id="doc-001",
                status=IngestionStatus.DONE,
                storage_key="pdf/doc-001/test.pdf",
            )
        )

        response = servicer.GetDocumentStatus(
            chunking_pb2.GetDocumentStatusRequest(document_id="doc-001"),
            _FakeContext(),
        )

        assert response.document_id == "doc-001"
        assert response.status == IngestionStatus.DONE.value
        assert response.storage_key == "pdf/doc-001/test.pdf"

    def test_get_cards_maps_sections(self):
        (
            servicer,
            _process_use_case,
            _search_use_case,
            _enqueue_use_case,
            _delete_use_case,
            _status_use_case,
            cards_use_case,
            _quiz_use_case,
        ) = self._build_servicer()
        cards_use_case.execute.return_value = Ok(
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

        response = servicer.GetCards(
            chunking_pb2.GetCardsRequest(document_id="doc-001"),
            _FakeContext(),
        )

        assert response.document_id == "doc-001"
        assert response.total_cards == 1
        assert response.sections[0].heading_path == ["Chapter 1", "Matrices"]
        assert response.sections[0].cards[0].card_id == "card-001"

    def test_get_quiz_maps_questions(self):
        (
            servicer,
            _process_use_case,
            _search_use_case,
            _enqueue_use_case,
            _delete_use_case,
            _status_use_case,
            _cards_use_case,
            quiz_use_case,
        ) = self._build_servicer()
        quiz_use_case.execute.return_value = Ok(
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

        response = servicer.GetQuiz(
            chunking_pb2.GetQuizRequest(document_id="doc-001"),
            _FakeContext(),
        )

        assert response.document_id == "doc-001"
        assert response.total_questions == 1
        assert response.questions[0].question_id == "quiz-001"
        assert response.questions[0].correct_index == 0

    def test_delete_document_uses_delete_use_case(self):
        (
            servicer,
            _process_use_case,
            _search_use_case,
            _enqueue_use_case,
            delete_use_case,
            _status_use_case,
            _cards_use_case,
            _quiz_use_case,
        ) = self._build_servicer()
        delete_use_case.execute.return_value = Ok(
            DeleteDocumentResponse(
                document_id="doc-001",
                success=True,
                message="Delete scheduled for document doc-001",
            )
        )

        response = servicer.DeleteDocument(
            chunking_pb2.DeleteDocumentRequest(document_id="doc-001"),
            _FakeContext(),
        )

        assert response.success is True
        assert response.message == "Delete scheduled for document doc-001"
        delete_use_case.execute.assert_called_once()

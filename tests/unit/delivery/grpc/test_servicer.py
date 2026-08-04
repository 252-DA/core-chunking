import grpc
from unittest.mock import MagicMock

from document_chunk.application.dto.generation_dto import (
    CardSection,
    CardsResponse,
    DocumentStatusResponse,
    GenerateCurriculumQuizRequest,
    GenerateCurriculumQuizResponse,
    LessonCardItem,
    QuizQuestionItem,
    QuizResponse,
)
from document_chunk.application.dto.search_dto import SearchResponse
from document_chunk.application.use_cases.delete_document import DeleteDocumentResponse
from document_chunk.delivery.grpc.proto import chunking_pb2
from document_chunk.delivery.grpc.servicer import ChunkingServicer
from document_chunk.domain.ports.metadata_store import (
    IngestionStatus,
    StoredAssessment,
    StoredChapter,
    StoredCourse,
    StoredLearningOutcome,
)
from document_chunk.shared.result import Ok


class _FakeContext:
    def abort(self, code, message):
        raise AssertionError(f"unexpected grpc abort: {code} {message}")


class _RecordingContext:
    """Records grpc abort calls instead of raising — for legacy-blocked tests."""

    def __init__(self) -> None:
        self.aborted: tuple[object, str] | None = None

    def abort(self, code, message) -> None:
        self.aborted = (code, message)


class TestChunkingServicer:
    def _build_servicer(
        self,
        *,
        metadata_store=None,
        generate_curriculum_quiz_use_case=None,
    ):
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
            generate_curriculum_quiz_use_case=generate_curriculum_quiz_use_case,
            metadata_store=metadata_store,
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

    def test_delete_document_is_legacy_blocked(self):
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

        context = _RecordingContext()
        servicer.DeleteDocument(
            chunking_pb2.DeleteDocumentRequest(document_id="doc-001"),
            context,
        )

        code, message = context.aborted
        assert code == grpc.StatusCode.UNIMPLEMENTED
        assert "Core API" in message
        delete_use_case.execute.assert_not_called()

    def test_get_curriculum_uses_injected_metadata_store(self):
        metadata_store = MagicMock()
        metadata_store.get_curriculum.return_value = Ok(
            (
                StoredCourse(
                    course_id="course-001",
                    code="CS101",
                    title_vi="Nhap mon",
                ),
                [
                    StoredChapter(
                        chapter_id="chapter-001",
                        course_id="course-001",
                        code="CH1",
                        title="Introduction",
                    )
                ],
                [
                    StoredLearningOutcome(
                        lo_id="lo-001",
                        course_id="course-001",
                        code="LO1",
                        parent_code=None,
                        statement_vi="Giai thich khai niem",
                    )
                ],
                [
                    StoredAssessment(
                        assessment_id="assessment-001",
                        course_id="course-001",
                        code="A1",
                        name_vi="Bai kiem tra",
                    )
                ],
            )
        )
        servicer, *_ = self._build_servicer(metadata_store=metadata_store)

        response = servicer.GetCurriculum(
            chunking_pb2.GetCurriculumRequest(course_id="course-001"),
            _FakeContext(),
        )

        metadata_store.get_curriculum.assert_called_once_with("course-001")
        assert response.code == "CS101"
        assert response.chapters[0].chapter_id == "chapter-001"
        assert response.learning_outcomes[0].lo_id == "lo-001"
        assert response.assessments[0].assessment_id == "assessment-001"

    def test_generate_curriculum_quiz_is_legacy_blocked(self):
        generate_use_case = MagicMock()
        servicer, *_ = self._build_servicer(
            generate_curriculum_quiz_use_case=generate_use_case
        )

        context = _RecordingContext()
        servicer.GenerateCurriculumQuiz(
            chunking_pb2.GenerateCurriculumQuizRequest(
                course_id="course-001",
                target_kind="lo",
                target_code="LO1",
                style="midterm",
                bloom_level="apply",
                count=3,
            ),
            context,
        )

        code, message = context.aborted
        assert code == grpc.StatusCode.UNIMPLEMENTED
        assert "Core API" in message
        generate_use_case.execute.assert_not_called()

    def test_process_and_enqueue_and_ingest_are_legacy_blocked(self):
        servicer, process_uc, _search_uc, enqueue_uc, _delete_uc, *_ = self._build_servicer()

        for call in (
            lambda ctx: servicer.ProcessDocument(
                chunking_pb2.ProcessDocumentRequest(
                    file_data=b"pdf-bytes", file_name="a.pdf", document_id="doc-001"
                ),
                ctx,
            ),
            lambda ctx: servicer.EnqueueDocument(
                chunking_pb2.ProcessDocumentRequest(
                    file_data=b"pdf-bytes", file_name="a.pdf", document_id="doc-001"
                ),
                ctx,
            ),
            lambda ctx: servicer.IngestCurriculum(
                chunking_pb2.IngestCurriculumRequest(
                    file_data=b"xls", file_name="curriculum.xlsx", course_id="course-001"
                ),
                ctx,
            ),
        ):
            context = _RecordingContext()
            call(context)
            code, message = context.aborted
            assert code == grpc.StatusCode.UNIMPLEMENTED
            assert "Core API" in message

        process_uc.execute.assert_not_called()
        enqueue_uc.execute.assert_not_called()

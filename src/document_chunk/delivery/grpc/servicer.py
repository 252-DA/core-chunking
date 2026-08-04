"""
ChunkingServicer — gRPC method implementations.

Bridges gRPC ↔ application use cases.
No business logic here — only:
  request validation → DTO mapping → use_case.execute() → gRPC response mapping.

File transfer:
  Client sends file_data (bytes) in the request.
  Servicer writes to a temp file → passes path to use case → deletes temp file.
"""
from typing import Protocol

import grpc

from document_chunk.application.dto.generation_dto import (
    GenerateCurriculumQuizRequest,
    GenerateCurriculumQuizResponse,
)
from document_chunk.application.dto.search_dto import SearchRequest
from document_chunk.application.use_cases.delete_document import DeleteDocumentUseCase
from document_chunk.application.use_cases.enqueue_document import EnqueueDocumentUseCase
from document_chunk.application.use_cases.get_cards import GetCardsRequest, GetCardsUseCase
from document_chunk.application.use_cases.get_document_status import (
    GetDocumentStatusRequest,
    GetDocumentStatusUseCase,
)
from document_chunk.application.use_cases.get_quiz import GetQuizRequest, GetQuizUseCase
from document_chunk.application.use_cases.ingest_curriculum import IngestCurriculumUseCase
from document_chunk.application.use_cases.process_document import ProcessDocumentUseCase
from document_chunk.application.use_cases.search_by_learning_outcome import (
    SearchByLearningOutcomeRequest,
    SearchByLearningOutcomeUseCase,
)
from document_chunk.application.use_cases.search_chunks import SearchChunksUseCase
from document_chunk.delivery.grpc.proto import chunking_pb2, chunking_pb2_grpc
from document_chunk.domain.entities.document import DocumentType
from document_chunk.domain.ports.metadata_store import IMetadataStore
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Result
from document_chunk.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_VERSION = "1.0.0"


class GenerateCurriculumQuizExecutor(Protocol):
    def execute(
        self,
        request: GenerateCurriculumQuizRequest,
    ) -> Result[GenerateCurriculumQuizResponse, Exception]: ...


class ChunkingServicer(chunking_pb2_grpc.ChunkingServiceServicer):
    def __init__(
        self,
        process_use_case: ProcessDocumentUseCase,
        search_use_case: SearchChunksUseCase,
        enqueue_use_case: EnqueueDocumentUseCase,
        delete_use_case: DeleteDocumentUseCase,
        get_document_status_use_case: GetDocumentStatusUseCase,
        get_cards_use_case: GetCardsUseCase,
        get_quiz_use_case: GetQuizUseCase,
        ingest_curriculum_use_case: IngestCurriculumUseCase | None = None,
        search_by_lo_use_case: SearchByLearningOutcomeUseCase | None = None,
        generate_curriculum_quiz_use_case: GenerateCurriculumQuizExecutor | None = None,
        metadata_store: IMetadataStore | None = None,
    ) -> None:
        self._process = process_use_case
        self._search = search_use_case
        self._enqueue = enqueue_use_case
        self._delete = delete_use_case
        self._get_document_status = get_document_status_use_case
        self._get_cards = get_cards_use_case
        self._get_quiz = get_quiz_use_case
        self._ingest_curriculum = ingest_curriculum_use_case
        self._search_by_lo = search_by_lo_use_case
        self._generate_curriculum_quiz = generate_curriculum_quiz_use_case
        self._metadata_store = metadata_store

    # ------------------------------------------------------------------
    # ProcessDocument (sync)
    # ------------------------------------------------------------------

    def ProcessDocument(
        self,
        request: chunking_pb2.ProcessDocumentRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.ProcessDocumentResponse:
        # Legacy mutation endpoint — closed per ownership-split plan (Phase 0).
        # Document processing now flows through the Core API workflow.
        logger.warning(
            "grpc.ProcessDocument.legacy_blocked",
            document_id=request.document_id or "",
        )
        context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "ProcessDocument is disabled: use the Core API upload workflow instead",
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def Search(
        self,
        request: chunking_pb2.SearchRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.SearchResponse:
        with tracer.start_as_current_span("grpc.Search"):
            if not request.query.strip():
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "query is required")

            logger.info("grpc.Search.received", query=request.query, top_k=request.top_k)
            result = None

            try:
                dto = SearchRequest(
                    query=request.query,
                    top_k=request.top_k or 10,
                    score_threshold=request.score_threshold or 0.0,
                    doc_types=[DocumentType(dt) for dt in request.doc_types if dt],
                    document_ids=list(request.document_ids),
                    language=request.language or None,
                    course_id=request.course_id or None,
                    owner_id=request.owner_id or None,
                )
                result = self._search.execute(dto)
            except Exception as e:
                logger.error("grpc.Search.exception", error=str(e))
                context.abort(grpc.StatusCode.INTERNAL, str(e))

            if result is None:
                return

            if result.is_err():
                err_msg = str(result.error)
                logger.error("grpc.Search.failed", error=err_msg)
                context.abort(grpc.StatusCode.INTERNAL, err_msg)

            resp = result.unwrap()
            return chunking_pb2.SearchResponse(
                query=resp.query,
                results=[
                    chunking_pb2.SearchResultItem(
                        chunk_id=item.chunk_id,
                        document_id=item.document_id,
                        document_name=item.document_name,
                        doc_type=item.doc_type.value,
                        heading_path=item.heading_path,
                        content=item.content,
                        score=item.score,
                        rank=item.rank,
                        page_number=item.page_number or 0,
                    )
                    for item in resp.results
                ],
                total_found=resp.total_found,
                search_time_ms=resp.search_time_ms,
            )

    # ------------------------------------------------------------------
    # DeleteDocument
    # ------------------------------------------------------------------

    def DeleteDocument(
        self,
        request: chunking_pb2.DeleteDocumentRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.DeleteDocumentResponse:
        # Legacy mutation endpoint — closed per ownership-split plan (Phase 0).
        # Document deletion now flows through the Core API workflow.
        logger.warning(
            "grpc.DeleteDocument.legacy_blocked",
            document_id=request.document_id,
        )
        context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "DeleteDocument is disabled: use the Core API document workflow instead",
        )

    # ------------------------------------------------------------------
    # EnqueueDocument (async)
    # ------------------------------------------------------------------

    def EnqueueDocument(
        self,
        request: chunking_pb2.ProcessDocumentRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.EnqueueDocumentResponse:
        # Legacy mutation endpoint — closed per ownership-split plan (Phase 0).
        # Document processing now flows through the Core API workflow.
        logger.warning(
            "grpc.EnqueueDocument.legacy_blocked",
            document_id=request.document_id or "",
        )
        context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "EnqueueDocument is disabled: use the Core API upload workflow instead",
        )

    # ------------------------------------------------------------------
    # GetDocumentStatus
    # ------------------------------------------------------------------

    def GetDocumentStatus(
        self,
        request: chunking_pb2.GetDocumentStatusRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.GetDocumentStatusResponse:
        if not request.document_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "document_id is required")

        result = self._get_document_status.execute(
            GetDocumentStatusRequest(document_id=request.document_id)
        )

        if result.is_err():
            context.abort(grpc.StatusCode.INTERNAL, str(result.error))

        status_response = result.unwrap()
        if status_response is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Document {request.document_id} not found")

        return chunking_pb2.GetDocumentStatusResponse(
            document_id=request.document_id,
            status=status_response.status.value,
            error_msg=status_response.error_msg or "",
            storage_key=status_response.storage_key or "",
        )

    # ------------------------------------------------------------------
    # GetCards
    # ------------------------------------------------------------------

    def GetCards(
        self,
        request: chunking_pb2.GetCardsRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.GetCardsResponse:
        if not request.document_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "document_id is required")

        result = self._get_cards.execute(GetCardsRequest(document_id=request.document_id))
        if result.is_err():
            context.abort(grpc.StatusCode.INTERNAL, str(result.error))

        response = result.unwrap()
        if response is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Document {request.document_id} not found")

        return chunking_pb2.GetCardsResponse(
            document_id=response.document_id,
            sections=[
                chunking_pb2.CardGroup(
                    heading_path=section.heading_path,
                    cards=[
                        chunking_pb2.LessonCard(
                            card_id=card.card_id,
                            chunk_id=card.chunk_id,
                            heading_path=card.heading_path,
                            title=card.title,
                            bullets=card.bullets,
                            key_insight=card.key_insight or "",
                            card_index=card.card_index,
                        )
                        for card in section.cards
                    ],
                )
                for section in response.sections
            ],
            total_cards=response.total_cards,
        )

    # ------------------------------------------------------------------
    # GetQuiz
    # ------------------------------------------------------------------

    def GetQuiz(
        self,
        request: chunking_pb2.GetQuizRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.GetQuizResponse:
        if not request.document_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "document_id is required")

        result = self._get_quiz.execute(GetQuizRequest(document_id=request.document_id))
        if result.is_err():
            context.abort(grpc.StatusCode.INTERNAL, str(result.error))

        response = result.unwrap()
        if response is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Document {request.document_id} not found")

        return chunking_pb2.GetQuizResponse(
            document_id=response.document_id,
            questions=[
                chunking_pb2.QuizItem(
                    question_id=question.question_id,
                    chunk_id=question.chunk_id,
                    question=question.question,
                    choices=question.choices,
                    correct_index=question.correct_index,
                    explanation=question.explanation or "",
                    difficulty=question.difficulty,
                )
                for question in response.questions
            ],
            total_questions=response.total_questions,
        )

    # ------------------------------------------------------------------
    # IngestCurriculum
    # ------------------------------------------------------------------

    def IngestCurriculum(
        self,
        request: chunking_pb2.IngestCurriculumRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.IngestCurriculumResponse:
        # Legacy mutation endpoint — closed per ownership-split plan (Phase 0).
        # Curriculum import now flows through the Core API workflow.
        logger.warning(
            "grpc.IngestCurriculum.legacy_blocked",
            course_id=request.course_id,
            file_name=request.file_name,
        )
        context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "IngestCurriculum is disabled: curriculum mutations go through the Core API",
        )

    # ------------------------------------------------------------------
    # GetCurriculum
    # ------------------------------------------------------------------

    def GetCurriculum(
        self,
        request: chunking_pb2.GetCurriculumRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.GetCurriculumResponse:
        if not request.course_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "course_id is required")
        metadata_store = self._metadata_store
        if metadata_store is None:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Curriculum store not configured")
        assert metadata_store is not None

        result = metadata_store.get_curriculum(request.course_id)
        if result.is_err():
            context.abort(grpc.StatusCode.INTERNAL, str(result.error))

        data = result.unwrap()
        if data is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Curriculum for {request.course_id} not found")

        stored_course, chapters, los, assessments = data
        return chunking_pb2.GetCurriculumResponse(
            course_id=stored_course.course_id,
            code=stored_course.code,
            title_vi=stored_course.title_vi,
            chapters=[
                chunking_pb2.ChapterMsg(
                    chapter_id=c.chapter_id,
                    code=c.code,
                    title=c.title,
                    order_index=c.order_index,
                )
                for c in chapters
            ],
            learning_outcomes=[
                chunking_pb2.LearningOutcomeMsg(
                    lo_id=lo.lo_id,
                    code=lo.code,
                    parent_code=lo.parent_code or "",
                    statement_vi=lo.statement_vi,
                    statement_en=lo.statement_en or "",
                    bloom_level=lo.bloom_level or "",
                    cdio_level=lo.cdio_level or 0,
                )
                for lo in los
            ],
            assessments=[
                chunking_pb2.AssessmentMsg(
                    assessment_id=a.assessment_id,
                    code=a.code,
                    name_vi=a.name_vi,
                    category=a.category,
                    weight=a.weight or 0.0,
                )
                for a in assessments
            ],
        )

    # ------------------------------------------------------------------
    # SearchByLearningOutcome
    # ------------------------------------------------------------------

    def SearchByLearningOutcome(
        self,
        request: chunking_pb2.SearchByLearningOutcomeRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.SearchByLearningOutcomeResponse:
        if not request.course_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "course_id is required")
        if self._search_by_lo is None:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "SearchByLearningOutcome not configured")

        result = self._search_by_lo.execute(
            SearchByLearningOutcomeRequest(
                course_id=request.course_id,
                lo_code=request.lo_code or None,
                chapter_code=request.chapter_code or None,
                assessment_code=request.assessment_code or None,
                query=request.query or None,
                top_k=request.top_k or 10,
            )
        )

        if result.is_err():
            context.abort(grpc.StatusCode.INTERNAL, str(result.error))

        resp = result.unwrap()
        return chunking_pb2.SearchByLearningOutcomeResponse(
            course_id=resp.course_id,
            lo_ids_searched=resp.lo_ids_searched,
            results=[
                chunking_pb2.LOSearchResultItem(
                    chunk_id=item.chunk_id,
                    document_id=item.document_id,
                    heading_path=list(item.heading_path),
                    content_text=item.content_text or "",
                    page_number=item.page_number or 0,
                    lo_ids=item.lo_ids,
                    rank=item.rank,
                )
                for item in resp.results
            ],
            total_found=resp.total_found,
        )

    # ------------------------------------------------------------------
    # GenerateCurriculumQuiz
    # ------------------------------------------------------------------

    def GenerateCurriculumQuiz(
        self,
        request: chunking_pb2.GenerateCurriculumQuizRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.GetQuizResponse:
        # Legacy mutation endpoint — closed per ownership-split plan (Phase 0).
        # Quiz generation now flows through the Core API workflow.
        logger.warning(
            "grpc.GenerateCurriculumQuiz.legacy_blocked",
            course_id=request.course_id,
            target_kind=request.target_kind,
            target_code=request.target_code,
        )
        context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "GenerateCurriculumQuiz is disabled: use the Core API content-generation workflow",
        )

    # ------------------------------------------------------------------
    # HealthCheck
    # ------------------------------------------------------------------

    def HealthCheck(
        self,
        request: chunking_pb2.HealthCheckRequest,
        context: grpc.ServicerContext,
    ) -> chunking_pb2.HealthCheckResponse:
        return chunking_pb2.HealthCheckResponse(
            status="ok",
            version=_VERSION,
        )

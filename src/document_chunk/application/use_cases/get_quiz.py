from dataclasses import dataclass

from document_chunk.application.dto.generation_dto import QuizQuestionItem, QuizResponse
from document_chunk.domain.ports.metadata_store import IMetadataStore
from document_chunk.shared.result import Err, Ok, Result


@dataclass(frozen=True)
class GetQuizRequest:
    document_id: str


class GetQuizUseCase:
    def __init__(self, metadata_store: IMetadataStore) -> None:
        self._metadata_store = metadata_store

    def execute(self, request: GetQuizRequest) -> Result[QuizResponse | None, Exception]:
        document_result = self._metadata_store.get(request.document_id)
        if document_result.is_err():
            return Err(document_result.error)
        if document_result.unwrap() is None:
            return Ok(None)

        quiz_result = self._metadata_store.list_quiz_items(request.document_id)
        if quiz_result.is_err():
            return Err(quiz_result.error)

        questions = [
            QuizQuestionItem(
                question_id=stored.question_id,
                chunk_id=stored.primary_chunk_id,
                question=stored.question,
                choices=list(stored.choices),
                correct_index=stored.correct_index,
                explanation=stored.explanation,
                difficulty=stored.difficulty,
            )
            for stored in quiz_result.unwrap()
        ]

        return Ok(
            QuizResponse(
                document_id=request.document_id,
                questions=questions,
                total_questions=len(questions),
            )
        )

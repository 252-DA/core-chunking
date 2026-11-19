"""IngestCurriculumUseCase chỉ parse: không còn metadata store hay graph store để ghi."""
import inspect
from pathlib import Path
from unittest.mock import MagicMock

from document_chunk.application.use_cases.ingest_curriculum import (
    IngestCurriculumRequest,
    IngestCurriculumUseCase,
)
from document_chunk.domain.entities.curriculum import Course, Curriculum, ExtractionIssue
from document_chunk.shared.result import Err, Ok

_REQUEST = IngestCurriculumRequest(
    file_path=Path("/tmp/syllabus.pdf"), file_name="DCMH.pdf", course_id="course-1"
)


def _curriculum(*issues: ExtractionIssue) -> Curriculum:
    return Curriculum(
        course=Course(course_id="course-1", code="CO3011", title_vi="QLDA"),
        chapters=(),
        learning_outcomes=(),
        assessments=(),
        issues=issues,
    )


def _use_case(extracted):
    parser = MagicMock()
    parser.supported_extensions = [".pdf"]
    parser.parse.return_value = Ok("parsed")
    extractor = MagicMock()
    extractor.extract.return_value = extracted
    return IngestCurriculumUseCase(parsers=[parser], curriculum_extractor=extractor), extractor


def test_constructor_has_no_store_to_write_to():
    params = set(inspect.signature(IngestCurriculumUseCase.__init__).parameters)
    assert params == {"self", "parsers", "curriculum_extractor"}


def test_extract_passes_the_course_hint_and_returns_issues_too():
    blocking = ExtractionIssue(code="lo.none", severity="error", message="Không có LO")
    use_case, extractor = _use_case(Ok(_curriculum(blocking)))

    result = use_case.extract(_REQUEST)

    assert result.is_ok()
    assert result.unwrap().blocking_issues == (blocking,)
    assert extractor.extract.call_args.kwargs == {"course_id_hint": "course-1"}


def test_execute_returns_the_parsed_curriculum():
    curriculum = _curriculum(ExtractionIssue(code="lo.bloom_inferred", severity="warning", message="x"))
    use_case, _ = _use_case(Ok(curriculum))

    assert use_case.execute(_REQUEST).unwrap() is curriculum


def test_execute_refuses_blocking_issues():
    blocking = ExtractionIssue(code="lo.none", severity="error", message="Không có LO")
    use_case, _ = _use_case(Ok(_curriculum(blocking)))

    result = use_case.execute(_REQUEST)

    assert result.is_err()
    assert "lo.none" in str(result.error)


def test_extract_error_is_passed_through():
    use_case, _ = _use_case(Err(ValueError("PDF hỏng")))

    assert "PDF hỏng" in str(use_case.execute(_REQUEST).error)

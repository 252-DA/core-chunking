"""
Unit tests for GenerateCurriculumQuizUseCase — mock LLM.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# worker package lives outside packages-ai — add worker directory to path
_WORKER_DIR = str(Path(__file__).parent.parent.parent.parent / "worker")
if _WORKER_DIR not in sys.path:
    sys.path.insert(0, _WORKER_DIR)

from document_chunk.domain.ports.metadata_store import (
    StoredAssessment,
    StoredChapter,
    StoredChunkMetadata,
    StoredCourse,
    StoredLearningOutcome,
)
from document_chunk.shared.result import Ok
from worker.use_cases.generate_curriculum_quiz import (
    GenerateCurriculumQuizRequest,
    GenerateCurriculumQuizUseCase,
)

_FAKE_LLM_RESPONSE = json.dumps({
    "questions": [
        {
            "question": "Phân tích yêu cầu chức năng là gì?",
            "choices": ["A. Mô tả hệ thống", "B. Phân tích chức năng", "C. Thiết kế cơ sở dữ liệu", "D. Kiểm thử phần mềm"],
            "correct_index": 1,
            "explanation": "Phân tích yêu cầu chức năng là bước quan trọng.",
            "difficulty": "medium",
            "lo_alignment_rationale": "Directly tests L.O.3.1 about functional requirements analysis.",
            "source_chunk_ids": ["chunk-001"],
        },
        {
            "question": "Use case diagram dùng để làm gì?",
            "choices": ["A. Mô hình hóa yêu cầu", "B. Thiết kế DB", "C. Viết code", "D. Deploy"],
            "correct_index": 0,
            "explanation": "Use case diagram mô hình hóa yêu cầu chức năng.",
            "difficulty": "easy",
            "lo_alignment_rationale": "Tests understanding of UML use case for requirements.",
            "source_chunk_ids": ["chunk-001"],
        },
    ]
})

_STORED_COURSE = StoredCourse(
    course_id="CO3115",
    code="CO3115",
    title_vi="Phân tích và Thiết kế Hệ thống",
)

_STORED_LO = StoredLearningOutcome(
    lo_id="CO3115:L.O.3.1",
    course_id="CO3115",
    code="L.O.3.1",
    parent_code="L.O.3",
    statement_vi="Phân tích yêu cầu chức năng",
    statement_en="Analyze functional requirements",
    bloom_level="analyze",
)

_STORED_CHUNK = StoredChunkMetadata(
    chunk_id="chunk-001",
    document_id="doc-001",
    chunk_index=0,
    heading_path=("Chương 3", "3.1 Yêu cầu"),
    heading_level=2,
    content_length=200,
    content_text="Use case diagram là công cụ mô hình hóa yêu cầu chức năng...",
)


@pytest.fixture
def mock_llm() -> MagicMock:
    llm = MagicMock()
    llm.model_id = "gemini-test"
    llm.generate.return_value = Ok(_FAKE_LLM_RESPONSE)
    return llm


@pytest.fixture
def mock_store() -> MagicMock:
    store = MagicMock()
    store.get_curriculum.return_value = Ok((
        _STORED_COURSE,
        [StoredChapter(chapter_id="CO3115:CH3", course_id="CO3115", code="3", title="Phân tích", order_index=3)],
        [_STORED_LO],
        [StoredAssessment(assessment_id="CO3115:A.O.2", course_id="CO3115", code="A.O.2", name_vi="Kiểm tra giữa kỳ", category="midterm")],
    ))
    store.list_chunks_for_lo.return_value = Ok([_STORED_CHUNK])
    store.persist_enrichment_batch.return_value = Ok(None)
    store.persist_curriculum_quiz_items.return_value = Ok(None)
    store.list_los_by_chapter.return_value = Ok([_STORED_LO])
    store.list_los_by_assessment.return_value = Ok([_STORED_LO])
    return store


@pytest.fixture
def use_case(mock_store, mock_llm) -> GenerateCurriculumQuizUseCase:
    uc = GenerateCurriculumQuizUseCase(
        metadata_store=mock_store,
        llm_client=mock_llm,
    )
    # Patch _set_lo_id_on_quiz_items to no-op (no real DB)
    uc._set_lo_id_on_quiz_items = lambda *args, **kwargs: None
    return uc


def test_generate_by_lo_code_returns_questions(use_case):
    req = GenerateCurriculumQuizRequest(
        course_id="CO3115",
        target_kind="lo",
        target_code="L.O.3.1",
        count=5,
    )
    result = use_case.execute(req)
    assert result.is_ok(), str(result.error)
    resp = result.unwrap()
    assert resp.course_id == "CO3115"
    assert resp.lo_id == "CO3115:L.O.3.1"
    assert resp.quiz_count >= 2


def test_generate_by_chapter(use_case, mock_store):
    req = GenerateCurriculumQuizRequest(
        course_id="CO3115",
        target_kind="chapter",
        target_code="3",
        count=3,
    )
    result = use_case.execute(req)
    assert result.is_ok(), str(result.error)
    mock_store.list_los_by_chapter.assert_called_once_with("CO3115", "3")


def test_generate_by_assessment(use_case, mock_store):
    req = GenerateCurriculumQuizRequest(
        course_id="CO3115",
        target_kind="assessment",
        target_code="A.O.2",
        count=3,
    )
    result = use_case.execute(req)
    assert result.is_ok(), str(result.error)
    mock_store.list_los_by_assessment.assert_called_once_with("CO3115", "A.O.2")


def test_unknown_target_kind_returns_error(use_case):
    req = GenerateCurriculumQuizRequest(
        course_id="CO3115",
        target_kind="invalid",
        target_code="anything",
    )
    result = use_case.execute(req)
    assert result.is_err()


def test_no_curriculum_returns_error(use_case, mock_store):
    mock_store.get_curriculum.return_value = Ok(None)
    req = GenerateCurriculumQuizRequest(
        course_id="UNKNOWN",
        target_kind="lo",
        target_code="L.O.1",
    )
    result = use_case.execute(req)
    assert result.is_err()


def test_persist_enrichment_called(use_case, mock_store):
    req = GenerateCurriculumQuizRequest(
        course_id="CO3115",
        target_kind="lo",
        target_code="L.O.3.1",
        count=2,
    )
    use_case.execute(req)
    mock_store.persist_enrichment_batch.assert_called_once()
    call_kwargs = mock_store.persist_enrichment_batch.call_args
    quiz_items = call_kwargs[1].get("quiz_items") or call_kwargs[0][2]
    assert len(quiz_items) >= 2


def test_question_ids_returned(use_case):
    req = GenerateCurriculumQuizRequest(
        course_id="CO3115",
        target_kind="lo",
        target_code="L.O.3.1",
        count=2,
    )
    resp = use_case.execute(req).unwrap()
    assert len(resp.question_ids) >= 2
    for qid in resp.question_ids:
        assert len(qid) == 36  # UUID format

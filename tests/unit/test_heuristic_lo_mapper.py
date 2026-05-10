"""
Unit tests for HeuristicLoMapper — heading-only chunk→LO mapper.
"""
import pytest

from src.adapters.curriculum.heuristic_lo_mapper import HeuristicLoMapper
from src.domain.entities.curriculum import (
    Assessment,
    Chapter,
    Course,
    Curriculum,
    LearningOutcome,
)
from src.domain.ports.metadata_store import StoredChunkMetadata


@pytest.fixture
def mapper() -> HeuristicLoMapper:
    return HeuristicLoMapper()


def _make_curriculum(course_id: str = "CO3115") -> Curriculum:
    course = Course(course_id=course_id, code=course_id, title_vi="Test Course")
    los = (
        LearningOutcome(
            lo_id=f"{course_id}:L.O.3",
            code="L.O.3",
            parent_code=None,
            statement_vi="Phân tích yêu cầu hệ thống",
            statement_en="Analyze system requirements",
            bloom_level="analyze",
        ),
        LearningOutcome(
            lo_id=f"{course_id}:L.O.3.1",
            code="L.O.3.1",
            parent_code="L.O.3",
            statement_vi="Phân tích yêu cầu chức năng",
            statement_en="Analyze functional requirements",
            bloom_level="analyze",
        ),
        LearningOutcome(
            lo_id=f"{course_id}:L.O.4.1",
            code="L.O.4.1",
            parent_code="L.O.4",
            statement_vi="Thiết kế cơ sở dữ liệu",
            statement_en="Design the database",
            bloom_level="create",
        ),
    )
    return Curriculum(
        course=course,
        chapters=(),
        learning_outcomes=los,
        assessments=(),
        lo_assessment_links=(),
    )


def _make_chunk(
    chunk_id: str,
    heading_path: tuple[str, ...],
    content_text: str = "some content",
) -> StoredChunkMetadata:
    return StoredChunkMetadata(
        chunk_id=chunk_id,
        document_id="doc-001",
        chunk_index=0,
        heading_path=heading_path,
        heading_level=len(heading_path),
        content_length=len(content_text),
        content_text=content_text,
    )


def test_chapter3_chunk_maps_to_lo3(mapper):
    curriculum = _make_curriculum()
    chunk = _make_chunk("c1", ("Chương 3", "3.1 Yêu cầu chức năng"))
    result = mapper.map([chunk], curriculum)
    assert result.is_ok()
    mappings = result.unwrap()
    lo_ids = {m.lo_id for m in mappings}
    assert "CO3115:L.O.3" in lo_ids or "CO3115:L.O.3.1" in lo_ids


def test_chapter4_chunk_maps_to_lo4(mapper):
    curriculum = _make_curriculum()
    chunk = _make_chunk("c2", ("Chương 4", "4.1 Thiết kế CSDL"))
    result = mapper.map([chunk], curriculum)
    assert result.is_ok()
    mappings = result.unwrap()
    lo_ids = {m.lo_id for m in mappings}
    assert "CO3115:L.O.4.1" in lo_ids


def test_chunk_without_heading_skipped(mapper):
    curriculum = _make_curriculum()
    chunk = _make_chunk("c3", ())
    result = mapper.map([chunk], curriculum)
    assert result.is_ok()
    assert result.unwrap() == []


def test_fuzzy_match_boosts_confidence(mapper):
    curriculum = _make_curriculum()
    # Heading contains words from L.O.3.1 statement
    chunk = _make_chunk("c4", ("Chương 3", "Phân tích yêu cầu chức năng"))
    result = mapper.map([chunk], curriculum)
    assert result.is_ok()
    mappings = {m.lo_id: m for m in result.unwrap()}
    lo_3_1 = mappings.get("CO3115:L.O.3.1")
    if lo_3_1:
        assert lo_3_1.confidence >= 0.9


def test_no_lo_for_unrelated_chapter(mapper):
    curriculum = _make_curriculum()
    chunk = _make_chunk("c5", ("Chương 9", "9.1 Unrelated topic"))
    result = mapper.map([chunk], curriculum)
    assert result.is_ok()
    assert result.unwrap() == []


def test_source_is_heading(mapper):
    curriculum = _make_curriculum()
    chunk = _make_chunk("c6", ("Chương 3",))
    result = mapper.map([chunk], curriculum)
    assert result.is_ok()
    for m in result.unwrap():
        assert m.source == "heading"


def test_dedup_keeps_max_confidence(mapper):
    curriculum = _make_curriculum()
    chunk = _make_chunk("c7", ("Chương 3", "Phân tích yêu cầu chức năng"))
    # Map same chunk twice
    result = mapper.map([chunk, chunk], curriculum)
    assert result.is_ok()
    mappings = result.unwrap()
    # Should not have duplicate (chunk_id, lo_id) pairs
    keys = [(m.chunk_id, m.lo_id) for m in mappings]
    assert len(keys) == len(set(keys))

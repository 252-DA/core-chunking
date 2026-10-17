"""CourseToolsUseCase và khóa khái niệm: kho giả, không DB."""
from unittest.mock import MagicMock

from document_chunk.application.use_cases.course_tools import (
    MAX_CHUNK_CHARS,
    CourseToolsUseCase,
)
from document_chunk.domain.concept_keys import concept_key, quote_is_verbatim
from document_chunk.domain.ports.course_knowledge_store import (
    ConceptRelation,
    CourseDocumentInfo,
    KnowledgeChunk,
)
from document_chunk.domain.ports.metadata_store import (
    StoredAssessment,
    StoredChapter,
    StoredChapterLOLink,
    StoredCourse,
    StoredLearningOutcome,
)
from document_chunk.shared.result import Err, Ok


def _chunk(i: int, text: str = "nội dung", page: int | None = 3) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=f"c{i}", document_id="d1", sort_order=i,
        heading_path=("Chương 1", "Deadlock"), page_number=page, content=text,
    )


def _tools(knowledge=None, metadata=None) -> CourseToolsUseCase:
    return CourseToolsUseCase(knowledge or MagicMock(), metadata or MagicMock())


def test_concept_key_folds_accents_case_and_punctuation():
    assert concept_key("Điều kiện Deadlock!") == "dieu kien deadlock"
    assert concept_key("  Circular   Wait (chờ vòng) ") == "circular wait cho vong"
    assert concept_key("???") == ""


def test_quote_must_be_substring_after_normalization():
    source = "Deadlock xảy ra khi bốn điều kiện\ncùng đúng."
    assert quote_is_verbatim("khi bốn  điều kiện cùng đúng", source)
    assert not quote_is_verbatim("khi ba điều kiện cùng đúng", source)
    assert not quote_is_verbatim("ngắn", "ngắn gọn")  # dưới số ký tự tối thiểu


def test_page_lookup_caps_chunk_size_and_reports_total():
    knowledge = MagicMock()
    long = "x" * (MAX_CHUNK_CHARS + 50)
    knowledge.list_page_chunks.return_value = Ok([_chunk(1, long)] + [_chunk(i) for i in range(2, 13)])
    out = _tools(knowledge).get_document_page("d1", 3, "CO3137").unwrap()
    knowledge.list_page_chunks.assert_called_once_with("d1", 3, "CO3137")
    assert out["total_chunks"] == 12
    assert len(out["chunks"]) == 8
    assert out["chunks"][0]["truncated"] is True
    assert len(out["chunks"][0]["content"]) == MAX_CHUNK_CHARS


def test_page_lookup_rejects_page_zero():
    assert _tools().get_document_page("d1", 0).is_err()


def test_neighbors_clamp_window():
    knowledge = MagicMock()
    knowledge.get_neighbor_chunks.return_value = Ok([_chunk(i) for i in range(1, 4)])
    tools = _tools(knowledge)
    tools.get_neighbors("c2", window=99)
    knowledge.get_neighbor_chunks.assert_called_once_with("c2", 3)


def test_relations_use_normalized_key_and_clamp_limits():
    knowledge = MagicMock()
    knowledge.find_relations.return_value = Ok(
        [
            ConceptRelation(
                relation_id="r1", source_label="Hold and wait", relation_type="cause",
                target_label="Deadlock", evidence_quote="giữ và chờ gây ra deadlock",
                chunk_id="c1", document_id="d1", page_number=5, hop=1,
            )
        ]
    )
    out = _tools(knowledge).get_concept_relations("CO3137", "Deadlock!", hops=9, limit=500).unwrap()
    knowledge.find_relations.assert_called_once_with("CO3137", "deadlock", 2, 25)
    assert out["found"] == 1
    assert out["relations"][0]["relation"] == "cause"
    assert out["relations"][0]["page_number"] == 5


def test_relations_reject_empty_concept():
    assert _tools().get_concept_relations("CO3137", "   ").is_err()


def test_list_documents_passes_chapter_filter():
    knowledge = MagicMock()
    knowledge.list_course_documents.return_value = Ok(
        [CourseDocumentInfo("d1", "Ch1.pdf", "lecture", "1", 33, 20)]
    )
    out = _tools(knowledge).list_documents("CO3137", "1").unwrap()
    knowledge.list_course_documents.assert_called_once_with("CO3137", "1")
    assert out["documents"][0] == {
        "document_id": "d1", "title": "Ch1.pdf", "role": "lecture", "chapter_code": "1", "pages": 33,
    }


def test_outline_filters_by_chapter_and_lists_linked_los():
    metadata = MagicMock()
    course = StoredCourse(course_id="co", code="CO3137", title_vi="Dữ liệu lớn")
    chapters = [
        StoredChapter("ch1", "co", "1", "Giới thiệu", 1, topics="4V"),
        StoredChapter("ch2", "co", "2", "Lưu trữ", 2),
    ]
    los = [
        StoredLearningOutcome("lo11", "co", "L.O.1.1", None, "Giải thích dữ liệu lớn", bloom_level="understand"),
        StoredLearningOutcome("lo21", "co", "L.O.2.1", None, "So sánh lưu trữ"),
    ]
    assessments = [StoredAssessment("a1", "co", "A1.1", "Bài tập", category="assignment", weight=0.2)]
    metadata.get_curriculum.return_value = Ok((course, chapters, los, assessments))
    metadata.list_chapter_lo_links.return_value = Ok(
        [StoredChapterLOLink("ch1", "lo11"), StoredChapterLOLink("ch2", "lo21")]
    )
    out = _tools(metadata=metadata).get_outline("CO3137", "1").unwrap()
    assert [c["code"] for c in out["chapters"]] == ["1"]
    assert out["chapters"][0]["los"] == ["L.O.1.1"]
    assert [lo["code"] for lo in out["learning_outcomes"]] == ["L.O.1.1"]
    assert out["assessments"][0]["name"] == "Bài tập"


def test_outline_errors_when_no_curriculum():
    metadata = MagicMock()
    metadata.get_curriculum.return_value = Ok(None)
    assert _tools(metadata=metadata).get_outline("CO3137").is_err()


def test_store_error_is_propagated():
    knowledge = MagicMock()
    knowledge.list_page_chunks.return_value = Err(RuntimeError("db down"))
    assert _tools(knowledge).get_document_page("d1", 1).is_err()

"""
Gắn tài liệu với chương khi giảng viên không xếp module theo chương
("Slides", "References"): khớp nội dung + map LO theo vị trí tài liệu.
"""
from unittest.mock import MagicMock

from document_chunk.adapters.curriculum.embedding_chapter_matcher import EmbeddingChapterMatcher
from document_chunk.adapters.curriculum.heuristic_lo_mapper import HeuristicLoMapper
from document_chunk.application.use_cases.map_chunks_to_los import (
    MapChunksToLosRequest,
    MapChunksToLosUseCase,
    _sections,
)
from document_chunk.domain.entities.curriculum import (
    Chapter,
    ChapterLOLink,
    Course,
    Curriculum,
    LearningOutcome,
)
from document_chunk.domain.ports.chapter_matcher import ChapterMatch, ChapterProfile
from document_chunk.domain.ports.chunk_lo_mapper import MappingHints
from document_chunk.domain.ports.metadata_store import (
    StoredChapter,
    StoredChapterLOLink,
    StoredChunkMetadata,
    StoredCourse,
    StoredDocumentPlacement,
    StoredLearningOutcome,
)
from document_chunk.shared.result import Ok

COURSE = "course-1"


class KeywordEmbedder:
    """Embedding giả: mỗi chiều là một từ khoá — đủ để kiểm tra logic chọn chương."""

    AXES = ("big data", "min-hashing", "kafka")

    def embed(self, texts):
        return Ok([[float(axis in t.lower()) + 0.01 for axis in self.AXES] for t in texts])


PROFILES = [
    ChapterProfile(code="1", text="Giới thiệu. Introduction. What is Big data?"),
    ChapterProfile(code="2", text="Tìm kiếm tương tự. Finding similar items. Min-hashing"),
    ChapterProfile(code="7", text="Công cụ truyền dẫn dữ liệu. Kafka"),
]


def chunk(chunk_id, index, heading_path=(), content=""):
    return StoredChunkMetadata(
        chunk_id=chunk_id,
        document_id="doc",
        chunk_index=index,
        heading_path=tuple(heading_path),
        content_text=content,
    )


def curriculum():
    return Curriculum(
        course=Course(course_id=COURSE, code="CO3137", title_vi="Dữ liệu lớn"),
        chapters=(
            Chapter(chapter_id="ch1", code="1", title="Giới thiệu"),
            Chapter(chapter_id="ch2", code="2", title="Tìm kiếm tương tự"),
            Chapter(chapter_id="ch3", code="3", title="Giảm chiều dữ liệu"),
        ),
        learning_outcomes=(
            LearningOutcome(lo_id="lo11", code="L.O.1.1", parent_code="L.O.1", statement_vi="Hiểu vấn đề dữ liệu lớn"),
            LearningOutcome(lo_id="lo23", code="L.O.2.3", parent_code="L.O.2", statement_vi="Liệt kê giải pháp"),
            LearningOutcome(lo_id="lo12", code="L.O.1.2", parent_code="L.O.1", statement_vi="Liệt kê giảm chiều"),
        ),
        assessments=(),
        chapter_lo_links=(
            ChapterLOLink(chapter_code="1", lo_code="L.O.1.1"),
            ChapterLOLink(chapter_code="2", lo_code="L.O.2.3"),
            ChapterLOLink(chapter_code="3", lo_code="L.O.1.2"),
        ),
    )


# ---------------------------------------------------------------------------
# EmbeddingChapterMatcher
# ---------------------------------------------------------------------------

def test_matcher_picks_the_chapter_that_clearly_wins():
    matcher = EmbeddingChapterMatcher(KeywordEmbedder())
    [match] = matcher.match(["Lecture: Min-hashing and shingling"], PROFILES).unwrap()
    assert match.chapter_code == "2"
    assert match.runner_up_code in {"1", "7"}


def test_matcher_abstains_when_two_chapters_tie():
    matcher = EmbeddingChapterMatcher(KeywordEmbedder())
    [match] = matcher.match(["Big data pipelines with Kafka"], PROFILES).unwrap()
    assert match.chapter_code is None
    assert match.best_code in {"1", "7"}


def test_matcher_batches_large_inputs():
    embedder = MagicMock()
    embedder.embed.side_effect = lambda texts: Ok([[1.0, 0.0] for _ in texts])
    EmbeddingChapterMatcher(embedder).match(["x"] * 100, PROFILES)
    assert [len(call.args[0]) for call in embedder.embed.call_args_list] == [64, 39]


# ---------------------------------------------------------------------------
# HeuristicLoMapper + MappingHints
# ---------------------------------------------------------------------------

def test_document_chapter_maps_every_chunk_even_without_headings():
    chunks = [chunk("a", 0, ["Introduction to Big Data"]), chunk("b", 1)]
    mappings = HeuristicLoMapper().map(
        chunks, curriculum(), MappingHints(role="lecture", document_chapter="1")
    ).unwrap()
    assert {(m.chunk_id, m.lo_id, m.source) for m in mappings} == {
        ("a", "lo11", "document"),
        ("b", "lo11", "document"),
    }


def test_reference_ignores_book_chapter_numbers():
    """"Chapter 3" của sách tham khảo không phải chương 3 của học phần."""
    chunks = [chunk("a", 0, ["Chapter 3 Finding Similar Items"])]
    no_hints = HeuristicLoMapper().map(chunks, curriculum()).unwrap()
    assert {m.lo_id for m in no_hints} == {"lo12"}  # đoán theo số — sai

    as_reference = HeuristicLoMapper().map(
        chunks, curriculum(), MappingHints(role="reference")
    ).unwrap()
    assert as_reference == []

    matched = HeuristicLoMapper().map(
        chunks, curriculum(), MappingHints(role="reference", chunk_chapters={"a": "2"})
    ).unwrap()
    assert {(m.lo_id, m.source) for m in matched} == {("lo23", "section")}


# ---------------------------------------------------------------------------
# MapChunksToLosUseCase — vị trí tài liệu
# ---------------------------------------------------------------------------

def build_use_case(placement, chunks, matcher=None):
    store = MagicMock()
    store.get_curriculum.return_value = Ok((
        StoredCourse(course_id=COURSE, code="CO3137", title_vi="Dữ liệu lớn"),
        [
            StoredChapter(chapter_id="ch1", course_id=COURSE, code="1", title="Giới thiệu",
                          title_en="Introduction", topics="What is Big data?; Applications"),
            StoredChapter(chapter_id="ch2", course_id=COURSE, code="2", title="Tìm kiếm tương tự",
                          topics="Min-hashing; Locality sensitive hashing"),
            StoredChapter(chapter_id="ch14", course_id=COURSE, code="14", title="Ôn tập", title_en="Review"),
        ],
        [
            StoredLearningOutcome(lo_id="lo11", course_id=COURSE, code="L.O.1.1", parent_code="L.O.1",
                                  statement_vi="Hiểu vấn đề dữ liệu lớn"),
            StoredLearningOutcome(lo_id="lo23", course_id=COURSE, code="L.O.2.3", parent_code="L.O.2",
                                  statement_vi="Liệt kê giải pháp"),
        ],
        [],
    ))
    store.list_chapter_lo_links.return_value = Ok([
        StoredChapterLOLink(chapter_id="ch1", lo_id="lo11"),
        StoredChapterLOLink(chapter_id="ch2", lo_id="lo23"),
    ])
    store.list_chunks.return_value = Ok(chunks)
    store.get_document_placement.return_value = Ok(placement)
    store.set_document_chapter.return_value = Ok(True)
    store.delete_inferred_chunk_lo_mappings.return_value = Ok(3)
    store.upsert_chunk_lo_mappings.return_value = Ok(None)
    graph = MagicMock()
    graph.upsert_chunk_lo_mappings.return_value = Ok(None)
    use_case = MapChunksToLosUseCase(
        metadata_store=store, graph_store=graph, lo_mapper=HeuristicLoMapper(), chapter_matcher=matcher
    )
    return use_case, store


def test_lecture_without_chapter_is_placed_by_content_and_recorded():
    matcher = MagicMock()
    matcher.match.return_value = Ok([ChapterMatch(chapter_code="1", score=0.62, margin=0.11,
                                                  best_code="1", runner_up_code="2")])
    chunks = [chunk("a", 0, ["Introduction to Big Data"], "What's Big Data?")]
    use_case, store = build_use_case(
        StoredDocumentPlacement(document_id="doc", course_id=COURSE, role="lecture"), chunks, matcher
    )

    result = use_case.execute(MapChunksToLosRequest(document_id="doc", course_id=COURSE)).unwrap()

    assert result.mapping_count == 1
    profiles = matcher.match.call_args.args[1]
    assert "Min-hashing" in next(p.text for p in profiles if p.code == "2")
    # "Ôn tập" không gắn LO nào nên không tham gia khớp.
    assert [p.code for p in profiles] == ["1", "2"]
    code, provenance, score, reason = store.set_document_chapter.call_args.args[1:]
    assert (code, provenance, score) == ("1", "content", 0.62)
    assert "chương 1" in reason
    [persisted] = store.upsert_chunk_lo_mappings.call_args.args[0]
    assert persisted.lo_id == "lo11"


def test_confirmed_chapter_is_used_without_matching():
    matcher = MagicMock()
    use_case, store = build_use_case(
        StoredDocumentPlacement(document_id="doc", course_id=COURSE, role="lecture",
                                chapter_code="2", chapter_provenance="confirmed"),
        [chunk("a", 0, ["Slides"])],
        matcher,
    )

    use_case.execute(MapChunksToLosRequest(document_id="doc", course_id=COURSE))

    matcher.match.assert_not_called()
    [persisted] = store.upsert_chunk_lo_mappings.call_args.args[0]
    assert persisted.lo_id == "lo23"


def test_stale_mappings_are_cleared_even_when_nothing_maps():
    matcher = MagicMock()
    matcher.match.return_value = Ok([ChapterMatch(chapter_code=None, score=0.4, margin=0.01)])
    use_case, store = build_use_case(
        StoredDocumentPlacement(document_id="doc", course_id=COURSE, role="lecture"),
        [chunk("a", 0, ["Misc"])],
        matcher,
    )

    result = use_case.execute(MapChunksToLosRequest(document_id="doc", course_id=COURSE)).unwrap()

    assert result.mapping_count == 0
    store.delete_inferred_chunk_lo_mappings.assert_called_once_with("doc")
    store.set_document_chapter.assert_not_called()
    store.upsert_chunk_lo_mappings.assert_not_called()


def test_reference_is_matched_section_by_section_with_strict_thresholds():
    matcher = MagicMock()
    matcher.match.return_value = Ok([
        ChapterMatch(chapter_code="2", score=0.7, margin=0.2),
        ChapterMatch(chapter_code=None, score=0.4, margin=0.01),
    ])
    chunks = [
        chunk("a", 0, ["Mining of Massive Datasets", "Finding Similar Items"], "shingling"),
        chunk("b", 1, ["Mining of Massive Datasets", "Finding Similar Items"], "minhash"),
        chunk("c", 2, ["Mining of Massive Datasets", "Clustering"], "k-means"),
    ]
    use_case, store = build_use_case(
        StoredDocumentPlacement(document_id="doc", course_id=COURSE, role="reference"), chunks, matcher
    )

    use_case.execute(MapChunksToLosRequest(document_id="doc", course_id=COURSE))

    texts = matcher.match.call_args.args[0]
    assert len(texts) == 2  # nhóm theo cấp 2 vì cả sách chung một heading gốc
    assert matcher.match.call_args.kwargs == {"strict": True}
    persisted = store.upsert_chunk_lo_mappings.call_args.args[0]
    assert {(m.chunk_id, m.lo_id) for m in persisted} == {("a", "lo23"), ("b", "lo23")}
    store.set_document_chapter.assert_not_called()


def test_sections_fall_back_to_fixed_windows_without_headings():
    chunks = [chunk(str(i), i) for i in range(20)]
    assert [len(s) for s in _sections(chunks)] == [8, 8, 4]

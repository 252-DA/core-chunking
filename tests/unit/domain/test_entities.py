"""
Tests for domain entities — Document, Section, ParsedDocument, Chunk, Embedding, Search entities.
"""
from datetime import datetime, timezone

import pytest

from document_chunk.domain.entities.chunk import Chunk, ChunkMetadata
from document_chunk.domain.entities.document import (
    Document,
    DocumentType,
    ElementType,
    ParsedDocument,
    Section,
)
from document_chunk.domain.entities.embedding import Embedding
from document_chunk.domain.entities.search import (
    SearchFilter,
    SearchQuery,
    SearchResponse,
    SearchResult,
)


# ---------------------------------------------------------------------------
# DocumentType & ElementType
# ---------------------------------------------------------------------------

class TestDocumentType:
    def test_known_values(self):
        assert DocumentType.PDF == "pdf"
        assert DocumentType.DOCX == "docx"
        assert DocumentType.PPTX == "pptx"
        assert DocumentType.MARKDOWN == "markdown"

    def test_string_equality(self):
        assert DocumentType.PDF == "pdf"
        assert DocumentType("pdf") == DocumentType.PDF


class TestElementType:
    def test_known_values(self):
        assert ElementType.HEADING == "heading"
        assert ElementType.PARAGRAPH == "paragraph"
        assert ElementType.TABLE == "table"
        assert ElementType.LIST == "list"
        assert ElementType.SLIDE == "slide"
        assert ElementType.IMAGE == "image"
        assert ElementType.CODE == "code"
        assert ElementType.UNKNOWN == "unknown"


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------

class TestDocument:
    def test_creation_with_default_created_at(self, tmp_path):
        pdf = tmp_path / "a.pdf"
        pdf.write_text("")
        doc = Document(
            id="d1", name="a.pdf", path=pdf, doc_type=DocumentType.PDF,
            size_bytes=0, mime_type="application/pdf",
        )
        assert doc.id == "d1"
        assert doc.name == "a.pdf"
        assert doc.doc_type == DocumentType.PDF
        assert isinstance(doc.created_at, datetime)

    def test_frozen_prevents_mutation(self, tmp_path):
        pdf = tmp_path / "a.pdf"
        pdf.write_text("")
        doc = Document(
            id="d1", name="a.pdf", path=pdf, doc_type=DocumentType.PDF,
            size_bytes=0, mime_type="application/pdf",
        )
        with pytest.raises(Exception):
            doc.name = "b.pdf"  # type: ignore[misc]

    def test_equality_excluding_created_at(self, tmp_path):
        pdf1 = tmp_path / "a.pdf"
        pdf1.write_text("")
        pdf2 = tmp_path / "b.pdf"
        pdf2.write_text("")
        doc1 = Document(
            id="d1", name="a.pdf", path=pdf1, doc_type=DocumentType.PDF,
            size_bytes=0, mime_type="application/pdf",
        )
        doc2 = Document(
            id="d1", name="a.pdf", path=pdf1, doc_type=DocumentType.PDF,
            size_bytes=0, mime_type="application/pdf",
        )
        doc3 = Document(
            id="d2", name="b.pdf", path=pdf2, doc_type=DocumentType.PDF,
            size_bytes=0, mime_type="application/pdf",
        )
        # Documents with same ID but different created_at are not equal via __eq__
        # because created_at is part of the dataclass fields. Test field-wise equality.
        assert doc1.id == doc2.id
        assert doc1.name == doc2.name
        assert doc1.doc_type == doc2.doc_type
        assert doc1 != doc3


# ---------------------------------------------------------------------------
# Section
# ---------------------------------------------------------------------------

class TestSection:
    def test_basic_section(self):
        s = Section(content="hello", element_type=ElementType.PARAGRAPH)
        assert s.content == "hello"
        assert s.element_type == ElementType.PARAGRAPH
        assert s.heading is None
        assert s.heading_level == 0
        assert s.page_number is None
        assert s.is_toc is False

    def test_heading_section(self):
        s = Section(
            content="Introduction",
            element_type=ElementType.HEADING,
            heading="Introduction",
            heading_level=2,
            page_number=1,
        )
        assert s.heading == "Introduction"
        assert s.heading_level == 2
        assert s.page_number == 1
        assert s.element_type == ElementType.HEADING

    def test_section_with_images(self):
        s = Section(
            content="text",
            element_type=ElementType.PARAGRAPH,
            images=("img1.png", "img2.png"),
        )
        assert s.images == ("img1.png", "img2.png")

    def test_section_with_metadata(self):
        s = Section(
            content="text",
            element_type=ElementType.PARAGRAPH,
            metadata={"key": "value"},
        )
        assert s.metadata == {"key": "value"}

    def test_toc_section(self):
        s = Section(
            content="Table of Contents",
            element_type=ElementType.PARAGRAPH,
            is_toc=True,
        )
        assert s.is_toc is True

    def test_frozen_prevents_mutation(self):
        s = Section(content="hi", element_type=ElementType.PARAGRAPH)
        with pytest.raises(Exception):
            s.content = "bye"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ParsedDocument
# ---------------------------------------------------------------------------

class TestParsedDocument:
    def test_total_content_length(self, sample_document):
        sections = [
            Section(content="abc", element_type=ElementType.PARAGRAPH),
            Section(content="defg", element_type=ElementType.PARAGRAPH),
        ]
        parsed = ParsedDocument(
            document=sample_document,
            sections=sections,
            page_count=1,
        )
        assert parsed.total_content_length == 7  # 3 + 4

    def test_total_content_length_empty(self, sample_document):
        parsed = ParsedDocument(
            document=sample_document,
            sections=[],
            page_count=0,
        )
        assert parsed.total_content_length == 0

    def test_headings(self, sample_document):
        sections = [
            Section(content="Chương 1", element_type=ElementType.HEADING, heading="Chương 1", heading_level=1),
            Section(content="text", element_type=ElementType.PARAGRAPH),
            Section(content="1.1 Mở đầu", element_type=ElementType.HEADING, heading="1.1 Mở đầu", heading_level=2),
        ]
        parsed = ParsedDocument(
            document=sample_document,
            sections=sections,
            page_count=1,
        )
        assert parsed.headings == ["Chương 1", "1.1 Mở đầu"]

    def test_headings_empty(self, sample_document):
        parsed = ParsedDocument(
            document=sample_document,
            sections=[Section(content="text", element_type=ElementType.PARAGRAPH)],
            page_count=1,
        )
        assert parsed.headings == []

    def test_language_and_metadata(self, sample_document):
        parsed = ParsedDocument(
            document=sample_document,
            sections=[],
            page_count=0,
            language="vi",
            metadata={"source": "DCMH"},
        )
        assert parsed.language == "vi"
        assert parsed.metadata == {"source": "DCMH"}


# ---------------------------------------------------------------------------
# ChunkMetadata & Chunk
# ---------------------------------------------------------------------------

class TestChunkMetadata:
    def test_minimal_creation(self):
        meta = ChunkMetadata(
            document_id="d1", document_name="f.pdf",
            document_type=DocumentType.PDF, chunk_index=0,
        )
        assert meta.document_id == "d1"
        assert meta.heading_path == ()
        assert meta.heading_level == 0
        assert meta.keywords == ()
        assert meta.entities == ()

    def test_full_creation(self):
        meta = ChunkMetadata(
            document_id="d1", document_name="f.pdf",
            document_type=DocumentType.PDF, chunk_index=5,
            heading_path=("Chương 1", "1.1"),
            heading_level=2,
            page_number=10,
            keywords=("AI", "ML"),
            language="vi",
        )
        assert meta.heading_path == ("Chương 1", "1.1")
        assert meta.heading_level == 2
        assert meta.keywords == ("AI", "ML")
        assert meta.language == "vi"

    def test_frozen(self):
        meta = ChunkMetadata(
            document_id="d1", document_name="f.pdf",
            document_type=DocumentType.PDF, chunk_index=0,
        )
        with pytest.raises(Exception):
            meta.document_id = "d2"  # type: ignore[misc]


class TestChunk:
    def test_document_id_property(self, sample_chunk):
        assert sample_chunk.document_id == "doc-001"

    def test_heading_path_str_single(self, sample_chunk):
        assert sample_chunk.heading_path_str == "Introduction"

    def test_heading_path_str_multiple(self, sample_document):
        chunk = Chunk(
            id="c2",
            content="test",
            embedding_input="test",
            content_hash="abc",
            metadata=ChunkMetadata(
                document_id="doc-001",
                document_name="test.pdf",
                document_type=DocumentType.PDF,
                chunk_index=0,
                heading_path=("Chapter 1", "Section 1.1", "Subsection"),
            ),
        )
        assert chunk.heading_path_str == "Chapter 1 > Section 1.1 > Subsection"

    def test_heading_path_str_empty(self, sample_document):
        chunk = Chunk(
            id="c3",
            content="test",
            embedding_input="test",
            content_hash="abc",
            metadata=ChunkMetadata(
                document_id="doc-001",
                document_name="test.pdf",
                document_type=DocumentType.PDF,
                chunk_index=0,
            ),
        )
        assert chunk.heading_path_str == ""

    def test_default_values(self, sample_document):
        chunk = Chunk(
            id="c4",
            content="test",
            embedding_input="test",
            content_hash="abc",
            metadata=ChunkMetadata(
                document_id="doc-001",
                document_name="test.pdf",
                document_type=DocumentType.PDF,
                chunk_index=0,
            ),
        )
        assert chunk.images == []
        assert chunk.embedding is None
        assert chunk.embedding_model is None
        assert chunk.score is None
        assert chunk.chunk_version == 1


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

class TestEmbedding:
    def test_valid_creation(self):
        emb = Embedding(
            chunk_id="c1",
            vector=tuple([0.1] * 128),
            model="test-model",
            dimension=128,
        )
        assert emb.chunk_id == "c1"
        assert len(emb.vector) == 128
        assert emb.dimension == 128

    def test_dimension_mismatch_raises(self):
        with pytest.raises(ValueError, match="dimension mismatch"):
            Embedding(
                chunk_id="c1",
                vector=tuple([0.1] * 128),
                model="test-model",
                dimension=256,
            )

    def test_frozen(self):
        emb = Embedding(
            chunk_id="c1",
            vector=tuple([0.1] * 128),
            model="test-model",
            dimension=128,
        )
        with pytest.raises(Exception):
            emb.chunk_id = "c2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SearchFilter
# ---------------------------------------------------------------------------

class TestSearchFilter:
    def test_defaults(self):
        sf = SearchFilter()
        assert sf.doc_types == ()
        assert sf.document_ids == ()
        assert sf.language is None
        assert sf.course_id is None
        assert sf.owner_id is None
        assert sf.uploaded_after is None
        assert sf.uploaded_before is None
        assert sf.heading_path_contains is None

    def test_with_filters(self):
        dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        sf = SearchFilter(
            doc_types=(DocumentType.PDF,),
            language="vi",
            course_id="course-001",
            uploaded_after=dt,
            heading_path_contains="AI",
        )
        assert sf.doc_types == (DocumentType.PDF,)
        assert sf.language == "vi"
        assert sf.course_id == "course-001"
        assert sf.uploaded_after == dt
        assert sf.heading_path_contains == "AI"


# ---------------------------------------------------------------------------
# SearchQuery
# ---------------------------------------------------------------------------

class TestSearchQuery:
    def test_valid_query(self):
        sq = SearchQuery(text="machine learning")
        assert sq.text == "machine learning"
        assert sq.top_k == 10
        assert sq.score_threshold == 0.0
        assert isinstance(sq.filters, SearchFilter)

    def test_empty_text_raises(self):
        with pytest.raises(ValueError, match="empty"):
            SearchQuery(text="")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="empty"):
            SearchQuery(text="   ")

    def test_top_k_zero_raises(self):
        with pytest.raises(ValueError, match="top_k"):
            SearchQuery(text="test", top_k=0)

    def test_top_k_negative_raises(self):
        with pytest.raises(ValueError, match="top_k"):
            SearchQuery(text="test", top_k=-1)

    def test_score_threshold_out_of_range_raises(self):
        with pytest.raises(ValueError, match="score_threshold"):
            SearchQuery(text="test", score_threshold=1.5)

    def test_score_threshold_negative_raises(self):
        with pytest.raises(ValueError, match="score_threshold"):
            SearchQuery(text="test", score_threshold=-0.1)

    def test_custom_top_k(self):
        sq = SearchQuery(text="test", top_k=5, score_threshold=0.5)
        assert sq.top_k == 5
        assert sq.score_threshold == 0.5


# ---------------------------------------------------------------------------
# SearchResult
# ---------------------------------------------------------------------------

class TestSearchResult:
    def test_creation(self, sample_chunk):
        sr = SearchResult(chunk=sample_chunk, score=0.85, rank=1)
        assert sr.chunk == sample_chunk
        assert sr.score == 0.85
        assert sr.rank == 1

    def test_frozen(self, sample_chunk):
        sr = SearchResult(chunk=sample_chunk, score=0.5, rank=2)
        with pytest.raises(Exception):
            sr.score = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SearchResponse
# ---------------------------------------------------------------------------

class TestSearchResponse:
    def test_has_results_true(self, sample_search_result):
        query = SearchQuery(text="test")
        resp = SearchResponse(
            query=query,
            results=(sample_search_result,),
            total_found=1,
            search_duration_ms=10.5,
        )
        assert resp.has_results is True
        assert resp.total_found == 1
        assert resp.search_duration_ms == 10.5

    def test_has_results_false(self):
        query = SearchQuery(text="test")
        resp = SearchResponse(
            query=query,
            results=(),
            total_found=0,
            search_duration_ms=5.0,
        )
        assert resp.has_results is False

    def test_top_result(self, sample_search_result):
        query = SearchQuery(text="test")
        resp = SearchResponse(
            query=query,
            results=(sample_search_result,),
            total_found=1,
            search_duration_ms=1.0,
        )
        assert resp.top_result == sample_search_result

    def test_top_result_none(self):
        query = SearchQuery(text="test")
        resp = SearchResponse(
            query=query,
            results=(),
            total_found=0,
            search_duration_ms=1.0,
        )
        assert resp.top_result is None

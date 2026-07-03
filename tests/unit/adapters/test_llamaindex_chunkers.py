"""
Unit tests cho LlamaIndex-based chunkers.

Các test này verify rằng:
  1. LlamaIndex chunkers implement IChunker đúng interface
  2. Output Chunk objects có metadata hợp lệ
  3. Heading context được preserve (SentenceChunker, TokenChunker)
  4. Kết quả so sánh được với HeadingChunker (chunk count, size, structure)

Yêu cầu: pip install 'document-chunk[llamaindex]'
Nếu chưa cài → các test tự động skip.
"""
import pytest

llama_index = pytest.importorskip(
    "llama_index.core",
    reason="llama-index-core chưa cài. Chạy: pip install 'document-chunk[llamaindex]'",
)

from document_chunk.adapters.chunkers.heading_chunker import HeadingChunker
from document_chunk.adapters.chunkers.llamaindex_chunkers import (
    LlamaIndexSentenceChunker,
    LlamaIndexTokenChunker,
)
from document_chunk.domain.entities.document import Document, DocumentType, ElementType, ParsedDocument, Section
from document_chunk.infrastructure.config import ChunkerConfig, LlamaIndexChunkerConfig
from document_chunk.shared.result import Ok
from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def doc_pdf() -> ParsedDocument:
    """Document PDF nhiều heading và content đa dạng."""
    doc = Document(
        id="test-pdf-001",
        name="lecture_notes.pdf",
        path=Path("/tmp/lecture_notes.pdf"),
        doc_type=DocumentType.PDF,
        size_bytes=1024,
        mime_type="application/pdf",
    )
    sections = [
        Section(content="Chapter 1: Introduction to Machine Learning",
                element_type=ElementType.HEADING, heading="Chapter 1: Introduction to Machine Learning",
                heading_level=1, page_number=1),
        Section(content=(
            "Machine learning is a subset of artificial intelligence that enables computers "
            "to learn from data without being explicitly programmed. "
            "It has revolutionized many fields including computer vision, natural language processing, "
            "and recommendation systems. "
            "The field was formally established in the 1950s, with Arthur Samuel coining the term "
            "'machine learning' in 1959."
        ), element_type=ElementType.PARAGRAPH, page_number=1),
        Section(content=(
            "There are three main types of machine learning: supervised learning, "
            "unsupervised learning, and reinforcement learning. "
            "Each type has its own strengths and is suited for different types of problems."
        ), element_type=ElementType.PARAGRAPH, page_number=2),
        Section(content="1.1 Supervised Learning",
                element_type=ElementType.HEADING, heading="1.1 Supervised Learning",
                heading_level=2, page_number=2),
        Section(content=(
            "In supervised learning, the model is trained on labeled data. "
            "The algorithm learns to map input features to output labels. "
            "Common algorithms include linear regression, decision trees, and support vector machines. "
            "The key challenge is generalization — performing well on unseen data."
        ), element_type=ElementType.PARAGRAPH, page_number=2),
        Section(content="1.2 Unsupervised Learning",
                element_type=ElementType.HEADING, heading="1.2 Unsupervised Learning",
                heading_level=2, page_number=3),
        Section(content=(
            "Unsupervised learning works with unlabeled data. "
            "The algorithm must find patterns and structure on its own. "
            "Clustering algorithms like K-means and hierarchical clustering are common examples. "
            "Dimensionality reduction techniques like PCA also fall in this category."
        ), element_type=ElementType.PARAGRAPH, page_number=3),
        Section(content="Chapter 2: Neural Networks",
                element_type=ElementType.HEADING, heading="Chapter 2: Neural Networks",
                heading_level=1, page_number=4),
        Section(content=(
            "Neural networks are inspired by the structure of the human brain. "
            "They consist of layers of interconnected nodes called neurons. "
            "Deep learning uses neural networks with many hidden layers to learn complex representations. "
            "The breakthrough of deep learning in 2012 with AlexNet transformed computer vision."
        ), element_type=ElementType.PARAGRAPH, page_number=4),
        Section(content="2.1 Backpropagation",
                element_type=ElementType.HEADING, heading="2.1 Backpropagation",
                heading_level=2, page_number=5),
        Section(content=(
            "Backpropagation is the algorithm used to train neural networks. "
            "It computes gradients of the loss function with respect to each weight "
            "by applying the chain rule of calculus. "
            "Gradient descent then updates the weights to minimize the loss. "
            "Variants like SGD, Adam, and RMSprop improve convergence."
        ), element_type=ElementType.PARAGRAPH, page_number=5),
    ]
    return ParsedDocument(
        document=doc, sections=sections, page_count=5, language="en"
    )


@pytest.fixture
def heading_chunker() -> HeadingChunker:
    return HeadingChunker(ChunkerConfig(max_chunk_size=800, min_chunk_size=50, overlap_size=100))


@pytest.fixture
def llamaindex_config() -> LlamaIndexChunkerConfig:
    # chunk_size=200 tokens ≈ 800 chars để tương đương với heading_chunker fixture
    return LlamaIndexChunkerConfig(chunk_size=200, chunk_overlap=25)


@pytest.fixture
def sentence_chunker(llamaindex_config: LlamaIndexChunkerConfig) -> LlamaIndexSentenceChunker:
    return LlamaIndexSentenceChunker(llamaindex_config)


@pytest.fixture
def token_chunker(llamaindex_config: LlamaIndexChunkerConfig) -> LlamaIndexTokenChunker:
    return LlamaIndexTokenChunker(llamaindex_config)


# ---------------------------------------------------------------------------
# Interface compliance tests
# ---------------------------------------------------------------------------

class TestIChunkerCompliance:
    def test_sentence_chunker_supported_types(self, sentence_chunker):
        types = sentence_chunker.supported_types
        assert DocumentType.PDF in types
        assert DocumentType.DOCX in types
        assert DocumentType.PPTX in types
        assert DocumentType.MARKDOWN in types

    def test_token_chunker_supported_types(self, token_chunker):
        assert DocumentType.PDF in token_chunker.supported_types

    def test_sentence_chunker_returns_ok(self, sentence_chunker, doc_pdf):
        result = sentence_chunker.chunk(doc_pdf)
        assert result.is_ok(), f"Expected Ok but got Err: {result}"

    def test_token_chunker_returns_ok(self, token_chunker, doc_pdf):
        result = token_chunker.chunk(doc_pdf)
        assert result.is_ok(), f"Expected Ok but got Err: {result}"

    def test_chunks_are_non_empty(self, sentence_chunker, doc_pdf):
        chunks = sentence_chunker.chunk(doc_pdf).unwrap()
        assert len(chunks) > 0

    def test_chunk_ids_are_unique(self, sentence_chunker, doc_pdf):
        chunks = sentence_chunker.chunk(doc_pdf).unwrap()
        ids = [c.id for c in chunks]
        assert len(ids) == len(set(ids)), "Duplicate chunk IDs"

    def test_chunk_indices_are_sequential(self, sentence_chunker, doc_pdf):
        chunks = sentence_chunker.chunk(doc_pdf).unwrap()
        indices = [c.metadata.chunk_index for c in chunks]
        assert indices == list(range(len(chunks)))

    def test_chunk_content_hash_matches(self, sentence_chunker, doc_pdf):
        import hashlib
        chunks = sentence_chunker.chunk(doc_pdf).unwrap()
        for chunk in chunks:
            expected = hashlib.md5(chunk.content.encode()).hexdigest()
            assert chunk.content_hash == expected

    def test_metadata_document_id_propagated(self, sentence_chunker, doc_pdf):
        chunks = sentence_chunker.chunk(doc_pdf).unwrap()
        for chunk in chunks:
            assert chunk.metadata.document_id == doc_pdf.document.id

    def test_metadata_language_propagated(self, sentence_chunker, doc_pdf):
        chunks = sentence_chunker.chunk(doc_pdf).unwrap()
        for chunk in chunks:
            assert chunk.metadata.language == "en"

    def test_char_count_matches_content(self, sentence_chunker, doc_pdf):
        chunks = sentence_chunker.chunk(doc_pdf).unwrap()
        for chunk in chunks:
            if chunk.metadata.char_count is not None:
                assert chunk.metadata.char_count == len(chunk.content)


# ---------------------------------------------------------------------------
# Heading context tests
# ---------------------------------------------------------------------------

class TestHeadingContextPreservation:
    def test_sentence_chunker_preserves_heading_in_embedding_input(self, sentence_chunker, doc_pdf):
        chunks = sentence_chunker.chunk(doc_pdf).unwrap()
        # Ít nhất một số chunk phải có heading context trong embedding_input
        chunks_with_heading = [
            c for c in chunks
            if c.metadata.heading_path and c.metadata.heading_path[0] in c.embedding_input
        ]
        assert len(chunks_with_heading) > 0, "Không có chunk nào có heading trong embedding_input"

    def test_sentence_chunker_preserves_heading_path(self, sentence_chunker, doc_pdf):
        chunks = sentence_chunker.chunk(doc_pdf).unwrap()
        paths = [c.metadata.heading_path for c in chunks if c.metadata.heading_path]
        assert len(paths) > 0, "Không có chunk nào có heading_path"

    def test_token_chunker_preserves_heading_path(self, token_chunker, doc_pdf):
        chunks = token_chunker.chunk(doc_pdf).unwrap()
        paths = [c.metadata.heading_path for c in chunks if c.metadata.heading_path]
        assert len(paths) > 0

    def test_heading_paths_contain_expected_headings(self, sentence_chunker, doc_pdf):
        chunks = sentence_chunker.chunk(doc_pdf).unwrap()
        all_paths = " | ".join(" > ".join(c.metadata.heading_path) for c in chunks)
        assert "Chapter 1" in all_paths or "Introduction" in all_paths


# ---------------------------------------------------------------------------
# Comparison tests: LlamaIndex vs HeadingChunker
# ---------------------------------------------------------------------------

class TestComparisonWithHeadingChunker:
    def test_all_chunkers_produce_chunks(self, heading_chunker, sentence_chunker, token_chunker, doc_pdf):
        hc_result = heading_chunker.chunk(doc_pdf).unwrap()
        sc_result = sentence_chunker.chunk(doc_pdf).unwrap()
        tc_result = token_chunker.chunk(doc_pdf).unwrap()

        assert len(hc_result) > 0
        assert len(sc_result) > 0
        assert len(tc_result) > 0

    def test_content_coverage_sentence_chunker(self, sentence_chunker, doc_pdf):
        """Toàn bộ nội dung quan trọng phải xuất hiện trong ít nhất một chunk."""
        chunks = sentence_chunker.chunk(doc_pdf).unwrap()
        all_content = " ".join(c.content for c in chunks)

        key_phrases = [
            "machine learning",
            "supervised learning",
            "backpropagation",
            "neural networks",
        ]
        for phrase in key_phrases:
            assert phrase.lower() in all_content.lower(), (
                f"Phrase '{phrase}' không tìm thấy trong bất kỳ chunk nào"
            )

    def test_content_coverage_token_chunker(self, token_chunker, doc_pdf):
        chunks = token_chunker.chunk(doc_pdf).unwrap()
        all_content = " ".join(c.content for c in chunks)
        assert "machine learning" in all_content.lower()
        assert "backpropagation" in all_content.lower()

    def test_no_empty_chunks(self, sentence_chunker, token_chunker, doc_pdf):
        for chunker in [sentence_chunker, token_chunker]:
            chunks = chunker.chunk(doc_pdf).unwrap()
            for chunk in chunks:
                assert chunk.content.strip(), f"{chunker.__class__.__name__} produced empty chunk"

    def test_chunk_size_within_expected_range(self, sentence_chunker, doc_pdf):
        """
        SentenceSplitter nên tạo chunks tương đối đồng đều về kích thước.
        Không có chunk nào quá lớn bất thường.
        """
        chunks = sentence_chunker.chunk(doc_pdf).unwrap()
        sizes = [len(c.content) for c in chunks]
        max_size = max(sizes)
        # ~200 tokens * 4 chars/token * 1.5 buffer = 1200 chars tối đa hợp lý
        assert max_size < 2000, f"Chunk quá lớn: {max_size} chars"

    def test_comparison_summary(self, heading_chunker, sentence_chunker, token_chunker, doc_pdf, capsys):
        """In bảng so sánh — không assert, chỉ để quan sát."""
        hc = heading_chunker.chunk(doc_pdf).unwrap()
        sc = sentence_chunker.chunk(doc_pdf).unwrap()
        tc = token_chunker.chunk(doc_pdf).unwrap()

        def stats(chunks):
            sizes = [len(c.content) for c in chunks]
            return {
                "count": len(chunks),
                "avg_size": sum(sizes) / len(sizes) if sizes else 0,
                "min_size": min(sizes) if sizes else 0,
                "max_size": max(sizes) if sizes else 0,
                "with_heading": sum(1 for c in chunks if c.metadata.heading_path),
            }

        hc_stats = stats(hc)
        sc_stats = stats(sc)
        tc_stats = stats(tc)

        print("\n\n=== Chunker Comparison ===")
        print(f"{'Metric':<20} {'HeadingChunker':>15} {'SentenceChunker':>15} {'TokenChunker':>15}")
        print("-" * 70)
        for key in ["count", "avg_size", "min_size", "max_size", "with_heading"]:
            print(f"{key:<20} {hc_stats[key]:>15.0f} {sc_stats[key]:>15.0f} {tc_stats[key]:>15.0f}")
        print()

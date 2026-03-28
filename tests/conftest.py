"""
Shared fixtures cho toàn bộ test suite.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.domain.entities.chunk import Chunk, ChunkMetadata
from src.domain.entities.document import Document, DocumentType, ElementType, ParsedDocument, Section
from src.domain.entities.embedding import Embedding
from src.domain.entities.search import SearchResult
from src.infrastructure.config import ChunkerConfig, QdrantConfig
from src.shared.result import Ok

# ---------------------------------------------------------------------------
# Domain entities
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_document(tmp_path: Path) -> Document:
    pdf_file = tmp_path / "test.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 sample")
    return Document(
        id="doc-001",
        name="test.pdf",
        path=pdf_file,
        doc_type=DocumentType.PDF,
        size_bytes=15,
        mime_type="application/pdf",
    )


@pytest.fixture
def sample_section() -> Section:
    return Section(
        content="This is a paragraph with some content for testing.",
        element_type=ElementType.PARAGRAPH,
        page_number=1,
    )


@pytest.fixture
def sample_heading_section() -> Section:
    return Section(
        content="Introduction",
        element_type=ElementType.HEADING,
        heading="Introduction",
        heading_level=1,
        page_number=1,
    )


@pytest.fixture
def sample_parsed_doc(sample_document: Document) -> ParsedDocument:
    return ParsedDocument(
        document=sample_document,
        sections=[
            Section(
                content="Introduction",
                element_type=ElementType.HEADING,
                heading="Introduction",
                heading_level=1,
                page_number=1,
            ),
            Section(
                content="This is the introduction paragraph with enough content.",
                element_type=ElementType.PARAGRAPH,
                page_number=1,
            ),
            Section(
                content="More content in the second paragraph here.",
                element_type=ElementType.PARAGRAPH,
                page_number=2,
            ),
        ],
        page_count=2,
        language="en",
    )


@pytest.fixture
def sample_chunk(sample_document: Document) -> Chunk:
    return Chunk(
        id="chunk-001",
        content="This is chunk content for testing purposes.",
        metadata=ChunkMetadata(
            document_id=sample_document.id,
            document_name=sample_document.name,
            doc_type=DocumentType.PDF,
            chunk_index=0,
            heading_path=("Introduction",),
            heading_level=1,
            page_number=1,
            language="en",
        ),
        enriched_content="Introduction\n\nThis is chunk content for testing purposes.",
    )


@pytest.fixture
def sample_embedding() -> Embedding:
    return Embedding(
        chunk_id="chunk-001",
        vector=tuple([0.1] * 1024),
        model="BAAI/bge-m3",
        dimension=1024,
    )


@pytest.fixture
def sample_search_result(sample_chunk: Chunk) -> SearchResult:
    return SearchResult(chunk=sample_chunk, score=0.95, rank=1)


# ---------------------------------------------------------------------------
# Config stubs
# ---------------------------------------------------------------------------

@pytest.fixture
def chunker_config() -> ChunkerConfig:
    return ChunkerConfig(max_chunk_size=500, min_chunk_size=50, overlap_size=50)


@pytest.fixture
def qdrant_config() -> QdrantConfig:
    return QdrantConfig(
        host="localhost",
        port=6333,
        collection_name="test_collection",
        vector_size=1024,
    )


# ---------------------------------------------------------------------------
# Mock ports
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_file_storage() -> MagicMock:
    storage = MagicMock()
    storage.upload.return_value = Ok("documents/pdf/doc-001/test.pdf")
    storage.download.return_value = Ok(None)
    storage.delete.return_value = Ok(None)
    storage.exists.return_value = True
    storage.get_url.return_value = Ok("http://minio/documents/pdf/doc-001/test.pdf")
    return storage


@pytest.fixture
def mock_vector_store(sample_search_result: SearchResult) -> MagicMock:
    store = MagicMock()
    store.upsert.return_value = Ok(None)
    store.search.return_value = Ok([sample_search_result])
    store.delete.return_value = Ok(None)
    store.delete_by_document.return_value = Ok(None)
    store.count.return_value = Ok(1)
    return store


@pytest.fixture
def mock_embedder(sample_embedding: Embedding) -> MagicMock:
    embedder = MagicMock()
    embedder.embed.return_value = Ok([[0.1] * 1024])
    embedder.embed_chunks.return_value = Ok([sample_embedding])
    return embedder


@pytest.fixture
def mock_parser(sample_parsed_doc: ParsedDocument) -> MagicMock:
    parser = MagicMock()
    parser.supports.return_value = True
    parser.parse.return_value = Ok(sample_parsed_doc)
    return parser


@pytest.fixture
def mock_chunker(sample_chunk: Chunk) -> MagicMock:
    chunker = MagicMock()
    chunker.chunk.return_value = Ok([sample_chunk])
    return chunker

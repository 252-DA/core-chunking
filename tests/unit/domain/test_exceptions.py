"""
Tests for domain exceptions hierarchy.
"""
import pytest

from document_chunk.domain.exceptions import (
    ChunkingError,
    ChunkError,
    EmbedError,
    FileStorageError,
    GraphStoreError,
    LLMError,
    MetadataStoreError,
    ParseError,
    ProcessingError,
    UnsupportedFileTypeError,
    VectorStoreError,
)


class TestChunkingError:
    def test_basic_message(self):
        err = ChunkingError("something went wrong")
        assert str(err) == "something went wrong"
        assert err.cause is None

    def test_with_cause(self):
        cause = ValueError("root cause")
        err = ChunkingError("wrapped error", cause=cause)
        assert "something went wrong" not in str(err)
        assert "caused by" in str(err)
        assert err.cause is cause

    def test_is_exception(self):
        err = ChunkingError("test")
        assert isinstance(err, Exception)


class TestParseError:
    def test_inherits_chunking_error(self):
        err = ParseError("parse failed")
        assert isinstance(err, ChunkingError)

    def test_message(self):
        err = ParseError("bad format")
        assert str(err) == "bad format"


class TestUnsupportedFileTypeError:
    def test_inherits_parse_error(self):
        err = UnsupportedFileTypeError(".xyz")
        assert isinstance(err, ParseError)

    def test_message_contains_file_type(self):
        err = UnsupportedFileTypeError(".xyz")
        assert ".xyz" in str(err)

    def test_file_type_attribute(self):
        err = UnsupportedFileTypeError(".xyz")
        assert err.file_type == ".xyz"


class TestChunkError:
    def test_inherits_chunking_error(self):
        assert isinstance(ChunkError("chunk failed"), ChunkingError)


class TestEmbedError:
    def test_inherits_chunking_error(self):
        assert isinstance(EmbedError("embed failed"), ChunkingError)


class TestVectorStoreError:
    def test_inherits_chunking_error(self):
        assert isinstance(VectorStoreError("qdrant down"), ChunkingError)


class TestFileStorageError:
    def test_inherits_chunking_error(self):
        assert isinstance(FileStorageError("minio down"), ChunkingError)


class TestMetadataStoreError:
    def test_inherits_chunking_error(self):
        assert isinstance(MetadataStoreError("pg down"), ChunkingError)


class TestGraphStoreError:
    def test_inherits_chunking_error(self):
        assert isinstance(GraphStoreError("neo4j down"), ChunkingError)


class TestLLMError:
    def test_inherits_chunking_error(self):
        assert isinstance(LLMError("llm timeout"), ChunkingError)


class TestProcessingError:
    def test_inherits_chunking_error(self):
        assert isinstance(ProcessingError("orchestration failed"), ChunkingError)

    def test_with_cause(self):
        cause = RuntimeError("inner")
        err = ProcessingError("orchestration failed", cause=cause)
        assert err.cause is cause

    def test_string_contains_cause(self):
        cause = ValueError("inner error")
        err = ProcessingError("wrap", cause=cause)
        assert "caused by" in str(err)
        assert "inner error" in str(err)


class TestExceptionRaising:
    def test_can_raise_catch_parse_error(self):
        with pytest.raises(ParseError):
            raise ParseError("test")

    def test_can_catch_as_base(self):
        with pytest.raises(ChunkingError):
            raise ParseError("test")

    def test_can_catch_as_exception(self):
        with pytest.raises(Exception):
            raise ProcessingError("test")

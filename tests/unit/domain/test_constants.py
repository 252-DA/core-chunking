"""
Tests for domain/constants.py — EXT_MAP and MIME_MAP.
"""
from document_chunk.domain.constants import EXT_MAP, MIME_MAP
from document_chunk.domain.entities.document import DocumentType


class TestExtMap:
    def test_pdf(self):
        assert EXT_MAP[".pdf"] == DocumentType.PDF

    def test_docx_doc(self):
        assert EXT_MAP[".docx"] == DocumentType.DOCX
        assert EXT_MAP[".doc"] == DocumentType.DOCX

    def test_pptx_ppt(self):
        assert EXT_MAP[".pptx"] == DocumentType.PPTX
        assert EXT_MAP[".ppt"] == DocumentType.PPTX

    def test_markdown(self):
        assert EXT_MAP[".md"] == DocumentType.MARKDOWN
        assert EXT_MAP[".markdown"] == DocumentType.MARKDOWN

    def test_unknown_extension(self):
        assert EXT_MAP.get(".xyz") is None
        assert EXT_MAP.get(".txt") is None
        assert EXT_MAP.get("") is None

    def test_case_sensitivity(self):
        assert ".PDF" not in EXT_MAP
        assert EXT_MAP.get(".PDF") is None


class TestMimeMap:
    def test_pdf(self):
        assert MIME_MAP[DocumentType.PDF] == "application/pdf"

    def test_docx(self):
        assert "wordprocessingml.document" in MIME_MAP[DocumentType.DOCX]

    def test_pptx(self):
        assert "presentationml.presentation" in MIME_MAP[DocumentType.PPTX]

    def test_markdown(self):
        assert MIME_MAP[DocumentType.MARKDOWN] == "text/markdown"

    def test_all_document_types_have_mime(self):
        for doc_type in DocumentType:
            assert doc_type in MIME_MAP

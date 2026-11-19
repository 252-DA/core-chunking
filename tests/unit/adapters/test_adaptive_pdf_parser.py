from unittest.mock import MagicMock, patch

from document_chunk.adapters.parsers.adaptive_pdf_parser import AdaptivePdfParser
from document_chunk.infrastructure.config import ParserConfig
from document_chunk.shared.result import Err, Ok


def test_uses_text_parser_without_loading_docling(sample_parsed_doc, tmp_path):
    parser = AdaptivePdfParser(ParserConfig())
    parser._text_parser.parse = MagicMock(return_value=Ok(sample_parsed_doc))

    with patch(
        "document_chunk.adapters.parsers.adaptive_pdf_parser.DoclingPdfParser"
    ) as docling:
        result = parser.parse(tmp_path / "text.pdf")

    assert result.is_ok()
    assert result.unwrap() is sample_parsed_doc
    docling.assert_not_called()


def test_falls_back_to_docling_when_text_extraction_fails(sample_parsed_doc, tmp_path):
    parser = AdaptivePdfParser(ParserConfig())
    parser._text_parser.parse = MagicMock(return_value=Err(RuntimeError("no text")))
    fallback = MagicMock()
    fallback.parse.return_value = Ok(sample_parsed_doc)

    with patch(
        "document_chunk.adapters.parsers.adaptive_pdf_parser.DoclingPdfParser",
        return_value=fallback,
    ):
        result = parser.parse(tmp_path / "scan.pdf")

    assert result.is_ok()
    fallback.parse.assert_called_once_with(tmp_path / "scan.pdf")

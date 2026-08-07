"""Choose a lightweight PDF parser first and reserve Docling for scanned PDFs."""

import gc
from pathlib import Path

from document_chunk.adapters.parsers.docling_pdf_parser import DoclingPdfParser
from document_chunk.adapters.parsers.pdf_parser import PdfParser
from document_chunk.domain.entities.document import DocumentType, ParsedDocument
from document_chunk.domain.ports.parser import IParser
from document_chunk.infrastructure.config import ParserConfig
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Result

logger = get_logger(__name__)


class AdaptivePdfParser(IParser):
    """Use PyMuPDF for text PDFs and Docling only when text extraction fails."""

    def __init__(self, config: ParserConfig) -> None:
        self._config = config
        self._text_parser = PdfParser(config)

    @property
    def supported_types(self) -> tuple[DocumentType, ...]:
        return (DocumentType.PDF,)

    def parse(self, path: Path) -> Result[ParsedDocument, Exception]:
        text_result = self._text_parser.parse(path)
        if text_result.is_ok():
            logger.info("adaptive_pdf_parser.selected", file=path.name, backend="pymupdf")
            return text_result

        logger.warning(
            "adaptive_pdf_parser.fallback",
            file=path.name,
            backend="docling",
            reason=str(text_result.error),
        )
        fallback = DoclingPdfParser(self._config)
        try:
            return fallback.parse(path)
        finally:
            # Docling owns several OCR/layout models. Do not retain them while
            # the BGE model is loaded for the next pipeline stage.
            del fallback
            gc.collect()

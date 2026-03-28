"""
Dependency Injection Container.

Wire tất cả dependencies lại với nhau — adapters → use cases.
Mỗi component là lazy singleton (tạo khi lần đầu được gọi).

Usage:
    from src.infrastructure.container import get_container

    container = get_container()
    use_case = container.process_document_use_case
"""
from functools import cached_property, lru_cache

from src.domain.ports.chunker import IChunker
from src.domain.ports.embedder import IEmbedder
from src.domain.ports.file_storage import IFileStorage
from src.domain.ports.parser import IParser
from src.domain.ports.vector_store import IVectorStore
from src.infrastructure.config import Settings, get_settings
from src.shared.logger import get_logger

logger = get_logger(__name__)


class Container:
    """
    Manual DI container — không dùng framework, đủ đơn giản để hiểu.

    Mỗi property là lazy singleton:
      - Chỉ khởi tạo khi lần đầu được access
      - Giữ nguyên instance cho các lần sau (cached_property)

    Khi adapters chưa implement → raise NotImplementedError rõ ràng
    thay vì crash lúc import.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    @cached_property
    def pdf_parser(self) -> IParser:
        from src.adapters.parsers.docling_pdf_parser import DoclingPdfParser
        logger.debug("container.init", component="DoclingPdfParser")
        return DoclingPdfParser(self._settings.parser)

    @cached_property
    def docx_parser(self) -> IParser:
        from src.adapters.parsers.docx_parser import DocxParser
        logger.debug("container.init", component="DocxParser")
        return DocxParser(self._settings.parser)

    @cached_property
    def pptx_parser(self) -> IParser:
        from src.adapters.parsers.pptx_parser import PptxParser
        logger.debug("container.init", component="PptxParser")
        return PptxParser(self._settings.parser)

    @cached_property
    def parsers(self) -> list[IParser]:
        """Tất cả parsers — dùng để resolve parser theo doc_type."""
        return [self.pdf_parser, self.docx_parser, self.pptx_parser]

    # ------------------------------------------------------------------
    # Chunker
    # ------------------------------------------------------------------

    @cached_property
    def chunker(self) -> IChunker:
        from src.adapters.chunkers.heading_chunker import HeadingChunker
        logger.debug("container.init", component="HeadingChunker")
        return HeadingChunker(self._settings.chunker)

    # ------------------------------------------------------------------
    # Embedder
    # ------------------------------------------------------------------

    @cached_property
    def embedder(self) -> IEmbedder:
        provider = self._settings.embedder.provider
        logger.debug("container.init", component="Embedder", provider=provider)

        if provider == "bge":
            from src.adapters.embedders.bge_embedder import BgeEmbedder
            return BgeEmbedder(self._settings.embedder)

        if provider == "openai":
            from src.adapters.embedders.openai_embedder import OpenAIEmbedder
            return OpenAIEmbedder(self._settings.embedder)

        raise ValueError(f"Unknown embedder provider: {provider}")

    # ------------------------------------------------------------------
    # Vector Store
    # ------------------------------------------------------------------

    @cached_property
    def vector_store(self) -> IVectorStore:
        from src.adapters.vector_db.qdrant_adapter import QdrantAdapter
        logger.debug("container.init", component="QdrantAdapter")
        return QdrantAdapter(self._settings.qdrant)

    # ------------------------------------------------------------------
    # File Storage
    # ------------------------------------------------------------------

    @cached_property
    def file_storage(self) -> IFileStorage:
        from src.adapters.storage.minio_adapter import MinioAdapter
        logger.debug("container.init", component="MinioAdapter")
        return MinioAdapter(self._settings.minio)

    # ------------------------------------------------------------------
    # Use Cases
    # ------------------------------------------------------------------

    @cached_property
    def process_document_use_case(self):
        from src.application.use_cases.process_document import ProcessDocumentUseCase
        logger.debug("container.init", component="ProcessDocumentUseCase")
        return ProcessDocumentUseCase(
            parsers=self.parsers,
            chunker=self.chunker,
            embedder=self.embedder,
            vector_store=self.vector_store,
            file_storage=self.file_storage,
        )

    @cached_property
    def search_chunks_use_case(self):
        from src.application.use_cases.search_chunks import SearchChunksUseCase
        logger.debug("container.init", component="SearchChunksUseCase")
        return SearchChunksUseCase(
            embedder=self.embedder,
            vector_store=self.vector_store,
        )


@lru_cache
def get_container() -> Container:
    """
    Singleton container — tạo một lần, dùng mãi.

    Usage:
        container = get_container()
        result = container.process_document_use_case.execute(request)
    """
    settings = get_settings()
    logger.info("container.created", env=settings.app.env)
    return Container(settings)

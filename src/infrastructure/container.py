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
from src.domain.ports.graph_store import IGraphStore
from src.domain.ports.llm_client import ILLMClient
from src.domain.ports.metadata_store import IMetadataStore
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

    def close(self) -> None:
        for component_name in ("graph_store", "metadata_store", "llm_client"):
            component = self.__dict__.get(component_name)
            close = getattr(component, "close", None)
            if not callable(close):
                continue

            try:
                close()
            except Exception as exc:
                logger.warning(
                    "container.close_failed",
                    component=component_name,
                    error=str(exc),
                )

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
    def markdown_parser(self) -> IParser:
        from src.adapters.parsers.markdown_parser import MarkdownParser
        logger.debug("container.init", component="MarkdownParser")
        return MarkdownParser(self._settings.parser)

    @cached_property
    def parsers(self) -> list[IParser]:
        """Tất cả parsers — dùng để resolve parser theo doc_type."""
        return [
            self.pdf_parser,
            self.docx_parser,
            self.pptx_parser,
            self.markdown_parser,
        ]

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
    # LLM Client
    # ------------------------------------------------------------------

    @cached_property
    def llm_client(self) -> ILLMClient:
        provider = self._settings.llm.provider
        logger.debug("container.init", component="LLMClient", provider=provider)

        if provider == "gemini":
            from src.adapters.llm.gemini_llm_client import GeminiLLMClient

            return GeminiLLMClient(self._settings.llm)

        raise ValueError(f"Unknown LLM provider: {provider}")

    # ------------------------------------------------------------------
    # Metadata Store (PostgreSQL)
    # ------------------------------------------------------------------

    @cached_property
    def metadata_store(self) -> IMetadataStore:
        if not self._settings.sql.enabled:
            from src.adapters.metadata.noop_metadata_store import NoopMetadataStore
            logger.warning("container.init", component="NoopMetadataStore", reason="sql.disabled")
            return NoopMetadataStore()

        from src.adapters.metadata.postgres_metadata_store import PostgresMetadataStore

        logger.debug("container.init", component="PostgresMetadataStore")
        return PostgresMetadataStore(self._settings.sql, self._settings.outbox)

    # ------------------------------------------------------------------
    # Graph Store (Neo4j)
    # ------------------------------------------------------------------

    @cached_property
    def graph_store(self) -> IGraphStore:
        if not self._settings.neo4j.enabled:
            from src.adapters.graph.noop_graph_store import NoopGraphStore
            logger.warning("container.init", component="NoopGraphStore", reason="neo4j.disabled")
            return NoopGraphStore()

        from src.adapters.graph.neo4j_graph_store import Neo4jGraphStore

        logger.debug("container.init", component="Neo4jGraphStore")
        return Neo4jGraphStore(self._settings.neo4j)

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
            metadata_store=self.metadata_store,
            job_queue=self.job_queue,
        )

    @cached_property
    def search_chunks_use_case(self):
        from src.application.use_cases.search_chunks import SearchChunksUseCase
        logger.debug("container.init", component="SearchChunksUseCase")
        return SearchChunksUseCase(
            embedder=self.embedder,
            vector_store=self.vector_store,
        )

    @cached_property
    def delete_document_use_case(self):
        from src.application.use_cases.delete_document import DeleteDocumentUseCase

        logger.debug("container.init", component="DeleteDocumentUseCase")
        return DeleteDocumentUseCase(
            metadata_store=self.metadata_store,
        )

    @cached_property
    def get_document_status_use_case(self):
        from src.application.use_cases.get_document_status import GetDocumentStatusUseCase

        logger.debug("container.init", component="GetDocumentStatusUseCase")
        return GetDocumentStatusUseCase(
            metadata_store=self.metadata_store,
        )

    @cached_property
    def get_cards_use_case(self):
        from src.application.use_cases.get_cards import GetCardsUseCase

        logger.debug("container.init", component="GetCardsUseCase")
        return GetCardsUseCase(
            metadata_store=self.metadata_store,
        )

    @cached_property
    def get_quiz_use_case(self):
        from src.application.use_cases.get_quiz import GetQuizUseCase

        logger.debug("container.init", component="GetQuizUseCase")
        return GetQuizUseCase(
            metadata_store=self.metadata_store,
        )

    @cached_property
    def job_queue(self):
        from src.adapters.queue.bullmq_adapter import BullMQAdapter
        logger.debug("container.init", component="BullMQAdapter")
        return BullMQAdapter(self._settings.redis)

    @cached_property
    def enqueue_document_use_case(self):
        from src.application.use_cases.enqueue_document import EnqueueDocumentUseCase
        logger.debug("container.init", component="EnqueueDocumentUseCase")
        return EnqueueDocumentUseCase(
            file_storage=self.file_storage,
            metadata_store=self.metadata_store,
            job_queue=self.job_queue,
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

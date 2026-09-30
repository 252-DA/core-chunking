"""MCP server exposing bounded learning-context retrieval tools."""

from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

from document_chunk.application.dto.search_dto import SearchRequest
from document_chunk.application.use_cases.retrieve_quiz_context import (
    RetrieveQuizContextRequest,
    RetrieveQuizContextUseCase,
)
from document_chunk.application.use_cases.search_chunks import SearchChunksUseCase
from document_chunk.infrastructure.config import McpConfig, get_settings
from document_chunk.infrastructure.container import get_container
from document_chunk.shared.logger import get_logger, setup_logging

logger = get_logger(__name__)


def create_mcp_server(
    retrieve_quiz_context_use_case: RetrieveQuizContextUseCase,
    search_chunks_use_case: SearchChunksUseCase,
    config: McpConfig | None = None,
) -> FastMCP:
    settings = config or McpConfig()
    server = FastMCP(
        "learning-context",
        instructions=(
            "Retrieve source-addressable course context for grounded quiz and "
            "learning-content generation."
        ),
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.path,
        stateless_http=True,
        json_response=True,
    )

    @server.tool()
    def retrieve_quiz_context(
        course_id: str,
        lo_code: str,
        query: str | None = None,
        bloom_level: str | None = None,
        assessment_style: str = "quiz",
        top_k: int = 5,
    ) -> dict:
        """Retrieve bounded, LO-aligned chunks to ground generated questions."""
        result = retrieve_quiz_context_use_case.execute(
            RetrieveQuizContextRequest(
                course_id=course_id,
                lo_code=lo_code,
                query=query,
                bloom_level=bloom_level,
                assessment_style=assessment_style,
                top_k=top_k,
            )
        )
        if result.is_err():
            raise ValueError(str(result.error))
        return asdict(result.unwrap())

    @server.tool()
    def search_course_chunks(
        course_id: str,
        query: str,
        top_k: int = 10,
        score_threshold: float = 0.0,
        document_ids: list[str] | None = None,
    ) -> dict:
        """Semantic-search course chunks and return content with source metadata.

        document_ids narrows the search to those documents — what a reader open on
        one document needs for a follow-up question. Empty means the whole course.
        """
        result = search_chunks_use_case.execute(
            SearchRequest(
                course_id=course_id,
                query=query,
                top_k=top_k,
                score_threshold=score_threshold,
                document_ids=list(document_ids or []),
            )
        )
        if result.is_err():
            raise ValueError(str(result.error))
        return result.unwrap().model_dump(mode="json")

    return server


def serve() -> None:
    settings = get_settings()
    setup_logging(
        level=settings.app.log_level,
        json_logs=settings.app.json_logs,
    )
    container = get_container()
    server = create_mcp_server(
        retrieve_quiz_context_use_case=container.retrieve_quiz_context_use_case,
        search_chunks_use_case=container.search_chunks_use_case,
        config=settings.mcp,
    )
    logger.info(
        "mcp.server.starting",
        address=f"http://{settings.mcp.host}:{settings.mcp.port}{settings.mcp.path}",
    )
    try:
        server.run(transport="streamable-http")
    finally:
        container.close()


if __name__ == "__main__":
    serve()

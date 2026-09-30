"""The MCP tool surface must expose the filters the layers below already support.

`search_course_chunks` hid `document_ids` even though SearchRequest → SearchFilter
→ the Qdrant filter builder all carry it. A reader open on one document could not
scope a follow-up question to that document, so these guard the wiring.
"""

from dataclasses import dataclass, field
from typing import Any

from document_chunk.application.dto.search_dto import SearchRequest
from document_chunk.delivery.mcp.server import create_mcp_server


@dataclass
class _Response:
    """Stand-in for SearchResponse — only model_dump() is used by the tool."""

    payload: dict[str, Any]

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return self.payload


@dataclass
class _Ok:
    value: _Response

    def is_err(self) -> bool:
        return False

    def unwrap(self) -> _Response:
        return self.value


@dataclass
class _RecordingSearchUseCase:
    seen: list[SearchRequest] = field(default_factory=list)

    def execute(self, request: SearchRequest) -> _Ok:
        self.seen.append(request)
        return _Ok(_Response({"results": [], "total_found": 0}))


def _server(search_use_case: _RecordingSearchUseCase):
    return create_mcp_server(
        retrieve_quiz_context_use_case=None,  # not exercised here
        search_chunks_use_case=search_use_case,
    )


async def test_search_course_chunks_advertises_document_ids() -> None:
    tools = await _server(_RecordingSearchUseCase()).list_tools()
    search = next(tool for tool in tools if tool.name == "search_course_chunks")

    assert "document_ids" in search.inputSchema["properties"]


async def test_search_course_chunks_forwards_document_ids() -> None:
    use_case = _RecordingSearchUseCase()

    await _server(use_case).call_tool(
        "search_course_chunks",
        {
            "course_id": "course-1",
            "query": "deadlock",
            "document_ids": ["doc-1", "doc-2"],
        },
    )

    assert use_case.seen[0].document_ids == ["doc-1", "doc-2"]


async def test_search_course_chunks_without_document_ids_stays_course_wide() -> None:
    use_case = _RecordingSearchUseCase()

    await _server(use_case).call_tool(
        "search_course_chunks",
        {"course_id": "course-1", "query": "deadlock"},
    )

    assert use_case.seen[0].document_ids == []

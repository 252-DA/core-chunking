"""
HTTP API with Swagger UI for manual chunking tests.

Open docs at:
    http://localhost:8000/docs
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from src.application.dto.document_dto import ProcessDocumentRequest, ProcessDocumentResponse
from src.application.dto.search_dto import SearchRequest, SearchResponse
from src.application.use_cases.delete_document import (
    DeleteDocumentRequest,
    DeleteDocumentUseCase,
)
from src.application.use_cases.process_document import ProcessDocumentUseCase
from src.application.use_cases.search_chunks import SearchChunksUseCase
from src.domain.exceptions import ChunkingError, UnsupportedFileTypeError
from src.infrastructure.config import get_settings
from src.infrastructure.container import Container, get_container
from src.shared.logger import get_logger, setup_logging
from src.shared.metrics import start_metrics_server
from src.shared.tracing import setup_tracing

logger = get_logger(__name__)

_VERSION = "1.0.0"


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = _VERSION
    docs_url: str = "/docs"


class DeleteDocumentHttpResponse(BaseModel):
    document_id: str
    success: bool
    message: str


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _parse_metadata_json(raw_metadata: str | None) -> dict[str, str]:
    metadata_text = _normalize_optional(raw_metadata)
    if metadata_text is None:
        return {}

    try:
        parsed = json.loads(metadata_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"metadata_json must be valid JSON: {exc}",
        ) from exc

    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="metadata_json must be a JSON object",
        )

    normalized: dict[str, str] = {}
    for key, value in parsed.items():
        if key is None or value is None:
            continue
        key_text = str(key).strip()
        value_text = str(value).strip()
        if key_text and value_text:
            normalized[key_text] = value_text
    return normalized


def _build_metadata(
    metadata_json: str | None,
    course_id: str | None,
    owner_id: str | None,
) -> dict[str, str]:
    metadata = _parse_metadata_json(metadata_json)
    course = _normalize_optional(course_id)
    owner = _normalize_optional(owner_id)
    if course is not None:
        metadata["course_id"] = course
    if owner is not None:
        metadata["owner_id"] = owner
    return metadata


def _raise_delivery_error(error: Exception) -> None:
    if isinstance(error, UnsupportedFileTypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        )
    if isinstance(error, ChunkingError):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=str(error),
    )


def create_app() -> FastAPI:
    settings = get_settings()

    setup_logging(
        level=settings.app.log_level,
        json_logs=settings.app.json_logs,
    )

    if settings.tracing.enabled:
        setup_tracing(
            service_name=settings.tracing.service_name,
            otlp_endpoint=settings.tracing.otlp_endpoint,
        )

    if settings.metrics.enabled:
        start_metrics_server(port=settings.metrics.port)

    container = get_container()

    app = FastAPI(
        title="Chunking HTTP API",
        summary="Upload a document and test chunking from Swagger UI.",
        version=_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.container = container

    @app.on_event("shutdown")
    def _shutdown() -> None:
        logger.info("http.server.shutting_down")
        container.close()

    return app


app = create_app()


def _get_container() -> Container:
    return app.state.container


def _get_process_use_case(
    container: Container = Depends(_get_container),
) -> ProcessDocumentUseCase:
    return container.process_document_use_case


def _get_search_use_case(
    container: Container = Depends(_get_container),
) -> SearchChunksUseCase:
    return container.search_chunks_use_case


def _get_delete_use_case(
    container: Container = Depends(_get_container),
) -> DeleteDocumentUseCase:
    return container.delete_document_use_case


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="Health check",
)
def health_check() -> HealthResponse:
    return HealthResponse()


@app.post(
    "/documents/process",
    response_model=ProcessDocumentResponse,
    tags=["documents"],
    summary="Upload and chunk a document",
)
async def process_document(
    file: Annotated[
        UploadFile,
        File(description="Document file to process, e.g. PDF, DOCX, PPTX, Markdown."),
    ],
    document_id: Annotated[
        str | None,
        Form(description="Optional document ID. Leave empty to auto-generate."),
    ] = None,
    language: Annotated[
        str | None,
        Form(description="Optional language, e.g. vi or en."),
    ] = None,
    course_id: Annotated[
        str | None,
        Form(description="Optional course ID stored as metadata."),
    ] = None,
    owner_id: Annotated[
        str | None,
        Form(description="Optional owner ID stored as metadata."),
    ] = None,
    metadata_json: Annotated[
        str | None,
        Form(
            description='Optional JSON object, e.g. {"semester":"2026A","tag":"demo"}.',
        ),
    ] = None,
    use_case: ProcessDocumentUseCase = Depends(_get_process_use_case),
) -> ProcessDocumentResponse:
    original_file_name = _normalize_optional(file.filename)
    if original_file_name is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="file name is required",
        )

    file_data = await file.read()
    if not file_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="uploaded file is empty",
        )

    metadata = _build_metadata(metadata_json, course_id, owner_id)
    suffix = Path(original_file_name).suffix or ".tmp"
    tmp_path: Path | None = None

    logger.info(
        "http.ProcessDocument.received",
        file_name=original_file_name,
        size_bytes=len(file_data),
    )

    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(file_data)
            tmp_path = Path(tmp.name)

        dto = ProcessDocumentRequest(
            file_path=tmp_path,
            document_id=_normalize_optional(document_id),
            original_file_name=original_file_name,
            language=_normalize_optional(language),
            metadata=metadata,
        )
        result = use_case.execute(dto)
        if result.is_err():
            _raise_delivery_error(result.error)

        return result.unwrap()

    finally:
        await file.close()
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()


@app.post(
    "/search",
    response_model=SearchResponse,
    tags=["search"],
    summary="Semantic search over chunks",
)
def search_chunks(
    request: SearchRequest,
    use_case: SearchChunksUseCase = Depends(_get_search_use_case),
) -> SearchResponse:
    result = use_case.execute(request)
    if result.is_err():
        _raise_delivery_error(result.error)
    return result.unwrap()


@app.delete(
    "/documents/{document_id}",
    response_model=DeleteDocumentHttpResponse,
    tags=["documents"],
    summary="Delete one document",
)
def delete_document(
    document_id: str,
    use_case: DeleteDocumentUseCase = Depends(_get_delete_use_case),
) -> DeleteDocumentHttpResponse:
    document_id = document_id.strip()
    if not document_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="document_id is required",
        )

    result = use_case.execute(DeleteDocumentRequest(document_id=document_id))
    if result.is_err():
        _raise_delivery_error(result.error)

    response = result.unwrap()
    return DeleteDocumentHttpResponse(
        document_id=response.document_id,
        success=response.success,
        message=response.message,
    )

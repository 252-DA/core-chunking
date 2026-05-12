"""
HTTP API with Swagger UI for manual chunking tests.

Open docs at:
    http://localhost:8000/docs
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from src.application.dto.document_dto import ProcessDocumentRequest, ProcessDocumentResponse
from src.application.dto.generation_dto import CardsResponse, DocumentStatusResponse, QuizResponse
from src.application.dto.search_dto import SearchRequest, SearchResponse
from src.application.use_cases.delete_document import (
    DeleteDocumentRequest,
    DeleteDocumentUseCase,
)
from src.application.use_cases.get_cards import GetCardsRequest, GetCardsUseCase
from src.application.use_cases.get_document_status import (
    GetDocumentStatusRequest,
    GetDocumentStatusUseCase,
)
from src.application.use_cases.get_quiz import GetQuizRequest, GetQuizUseCase
from src.application.use_cases.process_document import ProcessDocumentUseCase
from src.application.use_cases.search_chunks import SearchChunksUseCase
from src.domain.entities.document import DocumentType, ElementType
from src.domain.exceptions import ChunkingError, UnsupportedFileTypeError
from src.domain.ports.metadata_store import DocumentSummary, IMetadataStore
from src.domain.ports.parser import IParser
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


class DocumentSummaryDTO(BaseModel):
    """Lightweight document listing response for LMS / web admin."""
    document_id: str
    document_name: str
    doc_type: str
    status: str
    course_id: str | None
    created_at: str
    chunk_count: int

    @classmethod
    def from_domain(cls, ds: DocumentSummary) -> "DocumentSummaryDTO":
        return cls(
            document_id=ds.document_id,
            document_name=ds.document_name,
            doc_type=ds.doc_type,
            status=ds.status,
            course_id=ds.course_id,
            created_at=ds.created_at.isoformat(),
            chunk_count=ds.chunk_count,
        )


class SectionInspect(BaseModel):
    index: int
    element_type: str
    heading: str | None
    heading_level: int
    page_number: int | None
    content_length: int
    content_preview: str
    has_images: bool


class ParseStats(BaseModel):
    total_sections: int
    page_count: int
    element_counts: dict[str, int]
    image_count: int
    total_content_length: int
    headings_outline: list[str]
    language: str | None
    parser_used: str
    parse_duration_ms: float


class ParseInspectResponse(BaseModel):
    file_name: str
    file_size_bytes: int
    stats: ParseStats
    sections: list[SectionInspect]


class ChunkInspect(BaseModel):
    index: int
    chunk_id: str
    heading_path: list[str]
    page_number: int | None
    content_type: str | None
    is_toc: bool
    char_count: int
    content_preview: str
    embedding_input_preview: str


class ChunkingStats(BaseModel):
    total_chunks: int
    toc_chunks: int
    chunker_used: str
    chunk_size_tokens: int | None
    chunk_overlap_tokens: int | None
    avg_chars: float
    min_chars: int
    max_chars: int
    parse_duration_ms: float
    chunk_duration_ms: float


class ChunkInspectResponse(BaseModel):
    file_name: str
    file_size_bytes: int
    stats: ChunkingStats
    chunks: list[ChunkInspect]


_EXT_TO_DOC_TYPE: dict[str, DocumentType] = {
    ".pdf": DocumentType.PDF,
    ".docx": DocumentType.DOCX,
    ".pptx": DocumentType.PPTX,
    ".md": DocumentType.MARKDOWN,
    ".markdown": DocumentType.MARKDOWN,
}


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


def _get_status_use_case(
    container: Container = Depends(_get_container),
) -> GetDocumentStatusUseCase:
    return container.get_document_status_use_case


def _get_cards_use_case(
    container: Container = Depends(_get_container),
) -> GetCardsUseCase:
    return container.get_cards_use_case


def _get_quiz_use_case(
    container: Container = Depends(_get_container),
) -> GetQuizUseCase:
    return container.get_quiz_use_case


def _get_parsers(
    container: Container = Depends(_get_container),
) -> list[IParser]:
    return container.parsers


def _get_metadata_store(
    container: Container = Depends(_get_container),
) -> IMetadataStore:
    return container.metadata_store


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


@app.get(
    "/api/documents",
    response_model=list[DocumentSummaryDTO],
    tags=["lms"],
    summary="List documents for LMS / web admin",
    description="List document summaries with chunk count. Optional filter by course_id.",
)
def list_documents_endpoint(
    course_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    store: IMetadataStore = Depends(_get_metadata_store),
) -> list[DocumentSummaryDTO]:
    result = store.list_documents(course_id=course_id, limit=limit, offset=offset)
    if result.is_err():
        _raise_delivery_error(result.error)
    return [DocumentSummaryDTO.from_domain(s) for s in result.unwrap()]


@app.post(
    "/documents/inspect",
    response_model=ParseInspectResponse,
    tags=["inspect"],
    summary="Inspect parse quality — no storage, no chunking",
    description=(
        "Upload a document và xem raw parser output: sections, element types, headings, "
        "content previews. Không lưu DB, không chunk, không embed. "
        "Dùng để kiểm tra chất lượng parse trước khi full processing."
    ),
)
async def inspect_document(
    file: Annotated[
        UploadFile,
        File(description="PDF, DOCX, PPTX, hoặc Markdown để inspect."),
    ],
    preview_length: Annotated[
        int,
        Form(description="Số ký tự preview mỗi section (default 300, max 2000)."),
    ] = 300,
    parsers: list[IParser] = Depends(_get_parsers),
) -> ParseInspectResponse:
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

    preview_length = max(50, min(preview_length, 2000))

    suffix = Path(original_file_name).suffix.lower()
    doc_type = _EXT_TO_DOC_TYPE.get(suffix)
    if doc_type is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: '{suffix}'. Supported: {sorted(_EXT_TO_DOC_TYPE)}",
        )

    parser = next((p for p in parsers if p.supports(doc_type)), None)
    if parser is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"No parser registered for {doc_type.value}",
        )

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(file_data)
            tmp_path = Path(tmp.name)

        t0 = time.perf_counter()
        result = parser.parse(tmp_path)
        parse_ms = round((time.perf_counter() - t0) * 1000, 1)

        if result.is_err():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Parse failed: {result.error}",
            )

        parsed = result.unwrap()

    finally:
        await file.close()
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()

    element_counts: dict[str, int] = {}
    for s in parsed.sections:
        key = s.element_type.value
        element_counts[key] = element_counts.get(key, 0) + 1

    headings_outline = [
        f"{'  ' * max(0, s.heading_level - 1)}{'#' * s.heading_level} {s.content}"
        for s in parsed.sections
        if s.element_type == ElementType.HEADING
    ]

    section_inspects = [
        SectionInspect(
            index=i,
            element_type=s.element_type.value,
            heading=s.heading,
            heading_level=s.heading_level,
            page_number=s.page_number,
            content_length=len(s.content),
            content_preview=s.content[:preview_length],
            has_images=bool(s.images),
        )
        for i, s in enumerate(parsed.sections)
    ]

    return ParseInspectResponse(
        file_name=original_file_name,
        file_size_bytes=len(file_data),
        stats=ParseStats(
            total_sections=len(parsed.sections),
            page_count=parsed.page_count,
            element_counts=element_counts,
            image_count=len(parsed.images),
            total_content_length=parsed.total_content_length,
            headings_outline=headings_outline,
            language=parsed.language,
            parser_used=type(parser).__name__,
            parse_duration_ms=parse_ms,
        ),
        sections=section_inspects,
    )


@app.post(
    "/documents/inspect/chunking",
    response_model=ChunkInspectResponse,
    tags=["inspect"],
    summary="Inspect chunking quality — no storage, no embedding",
    description=(
        "Upload a document và xem chunk output của từng chunker strategy: "
        "**heading** (custom HeadingChunker), **sentence** (LlamaIndex SentenceSplitter), "
        "**token** (LlamaIndex TokenTextSplitter), **semantic** (LlamaIndex SemanticSplitter — chậm, cần GPU/CPU embed). "
        "Không lưu DB, không embed. Dùng để so sánh chất lượng chunking."
    ),
)
async def inspect_chunking(
    file: Annotated[
        UploadFile,
        File(description="PDF, DOCX, PPTX, hoặc Markdown để inspect."),
    ],
    chunker: Annotated[
        Literal["heading", "sentence", "token", "semantic"],
        Form(description="Chunker strategy: heading | sentence | token | semantic."),
    ] = "heading",
    chunk_size: Annotated[
        int,
        Form(description="Token chunk size cho LlamaIndex chunkers (default 375 ≈ 1500 chars). Bỏ qua với heading."),
    ] = 375,
    chunk_overlap: Annotated[
        int,
        Form(description="Token overlap cho LlamaIndex chunkers (default 50)."),
    ] = 50,
    preview_length: Annotated[
        int,
        Form(description="Số ký tự preview mỗi chunk (default 300, max 2000)."),
    ] = 300,
    parsers: list[IParser] = Depends(_get_parsers),
) -> ChunkInspectResponse:
    from src.adapters.chunkers.heading_chunker import HeadingChunker
    from src.infrastructure.config import ChunkerConfig, LlamaIndexChunkerConfig

    original_file_name = _normalize_optional(file.filename)
    if original_file_name is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="file name is required")

    file_data = await file.read()
    if not file_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="uploaded file is empty")

    preview_length = max(50, min(preview_length, 2000))
    chunk_size = max(50, min(chunk_size, 4096))
    chunk_overlap = max(0, min(chunk_overlap, chunk_size // 2))

    suffix = Path(original_file_name).suffix.lower()
    doc_type = _EXT_TO_DOC_TYPE.get(suffix)
    if doc_type is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: '{suffix}'. Supported: {sorted(_EXT_TO_DOC_TYPE)}",
        )

    parser = next((p for p in parsers if p.supports(doc_type)), None)
    if parser is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"No parser registered for {doc_type.value}",
        )

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(file_data)
            tmp_path = Path(tmp.name)

        t0 = time.perf_counter()
        parse_result = parser.parse(tmp_path)
        parse_ms = round((time.perf_counter() - t0) * 1000, 1)

        if parse_result.is_err():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Parse failed: {parse_result.error}",
            )

        parsed = parse_result.unwrap()

    finally:
        await file.close()
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()

    # Build chunker
    if chunker == "heading":
        _chunker = HeadingChunker(ChunkerConfig())
        chunk_size_out: int | None = None
        chunk_overlap_out: int | None = None
    else:
        from src.adapters.chunkers.llamaindex_chunkers import (
            LlamaIndexSemanticChunker,
            LlamaIndexSentenceChunker,
            LlamaIndexTokenChunker,
        )
        llama_cfg = LlamaIndexChunkerConfig(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        chunk_size_out = chunk_size
        chunk_overlap_out = chunk_overlap
        if chunker == "sentence":
            _chunker = LlamaIndexSentenceChunker(llama_cfg)
        elif chunker == "token":
            _chunker = LlamaIndexTokenChunker(llama_cfg)
        else:
            _chunker = LlamaIndexSemanticChunker(LlamaIndexChunkerConfig())

    t1 = time.perf_counter()
    chunk_result = _chunker.chunk(parsed)
    chunk_ms = round((time.perf_counter() - t1) * 1000, 1)

    if chunk_result.is_err():
        err = chunk_result.error
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        if "chưa được cài" in str(err) or "Thiếu dependency" in str(err):
            status_code = status.HTTP_501_NOT_IMPLEMENTED
        raise HTTPException(status_code=status_code, detail=str(err))

    chunks = chunk_result.unwrap()

    char_counts = [len(c.content) for c in chunks] if chunks else [0]
    toc_count = sum(1 for c in chunks if c.metadata.content_type == "toc")

    return ChunkInspectResponse(
        file_name=original_file_name,
        file_size_bytes=len(file_data),
        stats=ChunkingStats(
            total_chunks=len(chunks),
            toc_chunks=toc_count,
            chunker_used=type(_chunker).__name__,
            chunk_size_tokens=chunk_size_out,
            chunk_overlap_tokens=chunk_overlap_out,
            avg_chars=round(sum(char_counts) / len(char_counts), 1),
            min_chars=min(char_counts),
            max_chars=max(char_counts),
            parse_duration_ms=parse_ms,
            chunk_duration_ms=chunk_ms,
        ),
        chunks=[
            ChunkInspect(
                index=i,
                chunk_id=c.id,
                heading_path=list(c.metadata.heading_path),
                page_number=c.metadata.page_number,
                content_type=c.metadata.content_type,
                is_toc=c.metadata.content_type == "toc",
                char_count=len(c.content),
                content_preview=c.content[:preview_length],
                embedding_input_preview=c.embedding_input[:preview_length],
            )
            for i, c in enumerate(chunks)
        ],
    )


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


@app.get(
    "/documents/{document_id}/status",
    response_model=DocumentStatusResponse,
    tags=["documents"],
    summary="Get ingestion status for one document",
)
def get_document_status(
    document_id: str,
    use_case: GetDocumentStatusUseCase = Depends(_get_status_use_case),
) -> DocumentStatusResponse:
    result = use_case.execute(GetDocumentStatusRequest(document_id=document_id))
    if result.is_err():
        _raise_delivery_error(result.error)

    response = result.unwrap()
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"document not found: {document_id}",
        )
    return response


@app.get(
    "/documents/{document_id}/cards",
    response_model=CardsResponse,
    tags=["documents"],
    summary="Get generated lesson cards for one document",
)
def get_document_cards(
    document_id: str,
    use_case: GetCardsUseCase = Depends(_get_cards_use_case),
) -> CardsResponse:
    result = use_case.execute(GetCardsRequest(document_id=document_id))
    if result.is_err():
        _raise_delivery_error(result.error)

    response = result.unwrap()
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"document not found: {document_id}",
        )
    return response


@app.get(
    "/documents/{document_id}/quiz",
    response_model=QuizResponse,
    tags=["documents"],
    summary="Get generated quiz items for one document",
)
def get_document_quiz(
    document_id: str,
    use_case: GetQuizUseCase = Depends(_get_quiz_use_case),
) -> QuizResponse:
    result = use_case.execute(GetQuizRequest(document_id=document_id))
    if result.is_err():
        _raise_delivery_error(result.error)

    response = result.unwrap()
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"document not found: {document_id}",
        )
    return response


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

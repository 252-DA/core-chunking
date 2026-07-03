from document_chunk.shared.logger import bind_request_context, clear_request_context, get_logger, setup_logging
from document_chunk.shared.metrics import (
    ACTIVE_REQUESTS,
    CHUNKS_CREATED,
    CHUNK_SIZE,
    DOCUMENTS_PROCESSED,
    EMBEDDING_DURATION,
    PROCESSING_DURATION,
    start_metrics_server,
)
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_current_span_id, get_current_trace_id, get_tracer, setup_tracing

__all__ = [
    # logger
    "setup_logging",
    "get_logger",
    "bind_request_context",
    "clear_request_context",
    # tracing
    "setup_tracing",
    "get_tracer",
    "get_current_trace_id",
    "get_current_span_id",
    # metrics
    "start_metrics_server",
    "DOCUMENTS_PROCESSED",
    "CHUNKS_CREATED",
    "CHUNK_SIZE",
    "PROCESSING_DURATION",
    "EMBEDDING_DURATION",
    "ACTIVE_REQUESTS",
    # result
    "Ok",
    "Err",
    "Result",
]

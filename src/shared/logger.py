import logging
import sys
from typing import Any

import structlog


def _inject_trace_context(logger: Any, method: str, event_dict: dict) -> dict:
    """
    Structlog processor: inject trace_id + span_id from active OTel span into every log.
    Enables log-trace correlation in Grafana/Loki.
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"] = format(ctx.span_id, "016x")
    except ImportError:
        pass
    return event_dict


def _coerce_log_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)


def setup_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """
    Configure structlog for the application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_logs: True for JSON output (production), False for pretty output (dev)
    """
    log_level = _coerce_log_level(level)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

    shared_processors = [
        # Merge context vars bound via bind_contextvars() — useful for request_id, trace_id
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        # Inject trace_id + span_id from active OTel span
        _inject_trace_context,
    ]

    if json_logs:
        # Production: JSON output for log aggregators (Loki, ELK, Datadog)
        processors = shared_processors + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Development: colored human-readable output
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """
    Get a logger instance bound to a specific name/module.

    Usage:
        logger = get_logger(__name__)
        logger.info("parse.started", file="doc.pdf")
        logger.error("parse.failed", file="doc.pdf", exc_info=True)
    """
    return structlog.get_logger(name)


def bind_request_context(request_id: str, **kwargs) -> None:
    """
    Bind context to all subsequent log calls in the current thread/task.
    Useful for tracing a single request across multiple modules.

    Usage:
        bind_request_context(request_id="abc-123", file="doc.pdf")
        # All logs after this will include request_id and file automatically
    """
    structlog.contextvars.bind_contextvars(request_id=request_id, **kwargs)


def clear_request_context() -> None:
    """Clear bound context — call at the end of each request."""
    structlog.contextvars.clear_contextvars()

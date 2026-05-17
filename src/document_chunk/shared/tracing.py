from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from document_chunk.shared.logger import get_logger

logger = get_logger(__name__)


def setup_tracing(
    service_name: str = "document-chunk",
    otlp_endpoint: str | None = None,
    debug: bool = False,
) -> None:
    """
    Configure OpenTelemetry tracing.

    Args:
        service_name: Name of the service (shows up in Jaeger/Tempo)
        otlp_endpoint: OTLP HTTP endpoint, e.g. "http://localhost:4318/v1/traces"
                       If None, tracing is disabled in production or logged in debug.
        debug: If True, export spans to console (for local development)
    """
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if debug:
        # Dev: print spans to console
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        logger.debug("tracing.setup", mode="console")
    elif otlp_endpoint:
        # Production: export to Jaeger/Tempo via OTLP HTTP
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("tracing.setup", mode="otlp", endpoint=otlp_endpoint)
    else:
        logger.warning("tracing.disabled", reason="no otlp_endpoint provided")

    trace.set_tracer_provider(provider)


def get_tracer(name: str) -> trace.Tracer:
    """
    Get a tracer instance for a specific module.

    Usage:
        tracer = get_tracer(__name__)

        with tracer.start_as_current_span("parse_document") as span:
            span.set_attribute("file.name", filename)
            span.set_attribute("file.size_mb", size)
            # ... do work ...
    """
    return trace.get_tracer(name)


def get_current_trace_id() -> str | None:
    """
    Get the current trace ID as a hex string.
    Used to inject into logs for correlation.

    Returns None if no active span.
    """
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.is_valid:
        return format(ctx.trace_id, "032x")
    return None


def get_current_span_id() -> str | None:
    """Get the current span ID as a hex string."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.is_valid:
        return format(ctx.span_id, "016x")
    return None

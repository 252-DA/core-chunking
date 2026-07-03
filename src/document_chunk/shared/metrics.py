import threading

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from document_chunk.shared.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Metrics definitions
# ---------------------------------------------------------------------------

# Counters — chỉ tăng, không giảm
DOCUMENTS_PROCESSED = Counter(
    "documents_processed_total",
    "Total number of documents processed",
    ["status", "doc_type"],  # labels: status=success|failed, doc_type=pdf|docx|pptx
)

CHUNKS_CREATED = Counter(
    "chunks_created_total",
    "Total number of chunks created",
    ["doc_type"],
)

# Histograms — đo phân phối (latency, size)
PROCESSING_DURATION = Histogram(
    "document_processing_duration_seconds",
    "Time spent processing a document end-to-end",
    ["stage", "doc_type"],  # stage=parse|chunk|embed|store
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

CHUNK_SIZE = Histogram(
    "chunk_size_characters",
    "Size of chunks in characters",
    ["doc_type"],
    buckets=[100, 250, 500, 1000, 2000, 4000],
)

EMBEDDING_DURATION = Histogram(
    "embedding_duration_seconds",
    "Time spent generating embeddings",
    ["model"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# Gauges — giá trị có thể tăng/giảm
ACTIVE_REQUESTS = Gauge(
    "active_requests",
    "Number of documents currently being processed",
)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

_server_started = threading.Event()


def start_metrics_server(port: int = 9090) -> None:
    """
    Expose Prometheus metrics at http://localhost:{port}/metrics.
    Prometheus scrapes this endpoint periodically.

    Args:
        port: Port to expose metrics on (default: 9090)
    """
    if _server_started.is_set():
        logger.warning("metrics.server.already_running")
        return

    start_http_server(port)
    _server_started.set()
    logger.info("metrics.server.started", port=port, path="/metrics")

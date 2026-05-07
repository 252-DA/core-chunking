"""
gRPC Server — khởi động ChunkingService.

Usage:
    python -m src.delivery.grpc.server

Hoặc qua container:
    from src.delivery.grpc.server import serve
    serve()
"""
import signal
from concurrent import futures

import grpc

from src.delivery.grpc.proto import chunking_pb2_grpc
from src.delivery.grpc.servicer import ChunkingServicer
from src.infrastructure.config import get_settings
from src.infrastructure.container import get_container
from src.shared.logger import get_logger, setup_logging
from src.shared.metrics import start_metrics_server
from src.shared.tracing import setup_tracing

logger = get_logger(__name__)

# Tăng max message size lên 100MB để handle file lớn
_MAX_MESSAGE_LENGTH = 100 * 1024 * 1024  # 100 MB


def serve() -> None:
    settings = get_settings()

    # Setup observability trước khi làm gì khác
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

    # Wire dependencies
    container = get_container()

    servicer = ChunkingServicer(
        process_use_case=container.process_document_use_case,
        search_use_case=container.search_chunks_use_case,
        enqueue_use_case=container.enqueue_document_use_case,
        delete_use_case=container.delete_document_use_case,
        get_document_status_use_case=container.get_document_status_use_case,
        get_cards_use_case=container.get_cards_use_case,
        get_quiz_use_case=container.get_quiz_use_case,
    )

    # Build server
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=settings.grpc.max_workers),
        options=[
            ("grpc.max_send_message_length", _MAX_MESSAGE_LENGTH),
            ("grpc.max_receive_message_length", _MAX_MESSAGE_LENGTH),
        ],
    )

    chunking_pb2_grpc.add_ChunkingServiceServicer_to_server(servicer, server)

    address = f"{settings.grpc.host}:{settings.grpc.port}"
    server.add_insecure_port(address)

    # Graceful shutdown khi nhận SIGTERM / SIGINT
    def _shutdown(signum, frame):
        logger.info("grpc.server.shutting_down")
        server.stop(grace=5)  # non-blocking, wait_for_termination() sẽ return sau grace period

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    server.start()
    logger.info(
        "grpc.server.started",
        address=address,
        max_workers=settings.grpc.max_workers,
        env=settings.app.env,
    )

    server.wait_for_termination()
    container.close()
    logger.info("grpc.server.stopped")


if __name__ == "__main__":
    serve()

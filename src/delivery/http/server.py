"""
HTTP server entry point for the Swagger-based chunking API.

Usage:
    python -m src.delivery.http.server
"""
import uvicorn

from src.delivery.http.api import app
from src.infrastructure.config import get_settings


def serve() -> None:
    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.http.host,
        port=settings.http.port,
        log_config=None,
    )


if __name__ == "__main__":
    serve()

FROM ghcr.io/astral-sh/uv:0.6.14 AS uv
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    LLM__PROVIDER=gemini \
    LLM__MODEL=gemini-3-flash-preview

# System deps for python-magic, psycopg binary, etc.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libmagic1 \
        build-essential \
        zlib1g-dev \
        libgl1 \
        libglib2.0-0 \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-vie \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=uv /uv /uvx /bin/

COPY pyproject.toml uv.lock README.md ./

RUN uv sync --frozen --no-dev --extra embeddings --extra llamaindex --no-install-project

COPY src/ src/

EXPOSE 50051 8000 8001

ENTRYPOINT ["python", "-m"]
CMD ["document_chunk.delivery.grpc.server"]

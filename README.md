# Document Chunking Pipeline

A Python service for processing documents (PDF, DOCX, PPTX) into semantic chunks with vector embeddings, designed for RAG (Retrieval-Augmented Generation) systems.

Built on **Hexagonal Architecture (Ports & Adapters)** — clean separation between domain logic, adapters, and delivery.

## Architecture

```
src/
├── domain/                  # Core — không phụ thuộc gì từ ngoài
│   ├── entities/            # Document, Chunk, Embedding, Search
│   └── ports/               # Interfaces: IParser, IChunker, IEmbedder, IVectorStore, ...
├── application/             # Use cases + DTOs
│   ├── use_cases/           # ProcessDocument, SearchChunks
│   └── dto/                 # Request/Response contracts
├── adapters/                # Implementations của ports
│   ├── parsers/             # PDF, DOCX, PPTX
│   ├── preprocessors/       # OCR
│   ├── chunkers/            # HeadingChunker
│   ├── embedders/           # BGE-M3, OpenAI
│   ├── storage/             # MinIO
│   └── vector_db/           # Qdrant
├── delivery/                # Entry points
│   ├── grpc/                # gRPC server
│   ├── mcp/                 # MCP learning-context server
│   └── lib/                 # Library API
├── shared/                  # Logger, Tracing, Metrics, Result type
└── infrastructure/          # DI container, Config
```

### Data Flow

```
File Input
  → [Preprocessor] OCR nếu cần
  → [Parser]       File → ParsedDocument (structured sections)
  → [Chunker]      ParsedDocument → list[Chunk]
  → [Embedder]     list[Chunk] → list[Embedding]
  → [VectorStore]  Lưu vào Qdrant
  → [FileStorage]  Lưu raw file vào MinIO
```

## Supported Formats

| Format | Parser | Notes |
|--------|--------|-------|
| PDF | PyMuPDF | Digital + scanned (OCR) |
| DOCX | python-docx | Heading styles preserved |
| PPTX | python-pptx | Slide-based chunking |

## Supported Embedders

| Model | Dimension | Language |
|-------|-----------|----------|
| BGE-M3 | 1024 | Multilingual |
| OpenAI text-embedding-3-small | 1536 | Multilingual |
| OpenAI text-embedding-3-large | 3072 | Multilingual |

## Installation

Yêu cầu [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo-url>
cd packages-ai

# Install dependencies (tự tạo .venv)
uv sync

# Run
uv run python -m document_chunk.delivery.grpc.server
```

## MCP learning-context server

MCP is the model-facing retrieval boundary. It exposes bounded,
source-addressable context; it does not replace the internal gRPC API.

```bash
uv run python -m document_chunk.delivery.mcp.server
```

The Streamable HTTP endpoint is `http://localhost:8001/mcp` and currently
exposes:

- `retrieve_quiz_context`: resolves a real course LO and returns only chunks
  mapped to that LO, bounded by chunk count and total characters;
- `search_course_chunks`: semantic course-scoped retrieval with source
  metadata.

In the root compose stack, run:

```bash
docker compose up --build mcp-server content-generation-worker
```

## Observability

| Pillar | Stack |
|--------|-------|
| Logs | `structlog` — structured JSON |
| Traces | OpenTelemetry → Jaeger / Tempo |
| Metrics | `prometheus-client` → Grafana |

Trace ID tự động được inject vào logs để correlate.

## Configuration

Tất cả config load từ environment variables hoặc `.env` file.

```bash
cp .env.example .env
# Chỉnh sửa .env
```

Xem [src/infrastructure/config.py](src/infrastructure/config.py) để biết toàn bộ options.

## Roadmap

- [x] Domain entities + ports
- [x] Observability (logs, traces, metrics)
- [ ] PDF, DOCX, PPTX parsers
- [ ] Heading-based chunker
- [ ] BGE-M3 embedder
- [ ] Qdrant adapter
- [ ] MinIO adapter
- [ ] gRPC delivery
- [x] MCP learning-context delivery
- [ ] Neo4j metadata store (Phase 2)
- [ ] Hybrid search — dense + sparse (Phase 2)

## License

To be determined.

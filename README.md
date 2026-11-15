# Document Chunking Pipeline (`document-chunk`)

A Python library for processing documents (PDF, DOCX, PPTX) into semantic chunks with vector embeddings, designed for RAG (Retrieval-Augmented Generation) systems and consumed by `worker` and `mcp-service`.

Built on **Hexagonal Architecture (Ports & Adapters)** — clean separation between domain logic and infrastructure adapters.

## Architecture

```
src/document_chunk/
├── domain/                  # Core — không phụ thuộc gì từ ngoài
│   ├── entities/            # Document, Chunk, Embedding, Search
│   └── ports/               # Interfaces: IParser, IChunker, IEmbedder, IVectorStore, ...
├── application/             # Use cases + DTOs
│   ├── use_cases/           # PipelineCore, SearchChunks, MapChunksToLos, CourseTools, ...
│   └── dto/                 # Request/Response contracts
├── adapters/                # Implementations của ports
│   ├── parsers/             # PDF (PyMuPDF, Docling), DOCX, PPTX, Markdown
│   ├── chunkers/            # HeadingChunker, StructuralChunker, LlamaIndexChunker
│   ├── embedders/           # BGE-M3 (local / gRPC), OpenAI
│   ├── storage/             # MinIO
│   ├── vector_db/           # Qdrant
│   ├── graph_db/            # Neo4j
│   └── metadata/            # Postgres repositories
├── shared/                  # Logger, Tracing, Metrics, Result type
└── infrastructure/          # Config
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

## Installation & Testing

Yêu cầu [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo-url>
cd packages-ai

# Install dependencies (tự tạo .venv)
uv sync

# Run unit tests
uv run pytest tests/unit
```

Runtime entry points (workers, MCP server, HTTP/gRPC APIs) nằm ở các service ngoài dùng library này (`worker`, `mcp-service`, `core-api`).

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

Xem [src/document_chunk/infrastructure/config.py](src/document_chunk/infrastructure/config.py) để biết toàn bộ options.

## Roadmap

- [x] Domain entities + ports
- [x] Observability (logs, traces, metrics)
- [x] PDF, DOCX, PPTX, Markdown parsers
- [x] Heading / Structural chunkers
- [x] BGE-M3 (local + gRPC) & OpenAI embedders
- [x] Qdrant adapter
- [x] MinIO adapter
- [x] Neo4j graph adapter
- [ ] Hybrid search — dense + sparse (Phase 2)

## License

To be determined.

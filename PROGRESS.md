# Progress — Document Chunking Pipeline

> Cập nhật: 2026-03-18

---

## Architecture

**Hexagonal Architecture (Ports & Adapters)**

```
src/
├── domain/          # Core — không phụ thuộc gì từ ngoài
│   ├── entities/    # Data models bất biến
│   └── ports/       # Abstract interfaces (contracts)
├── application/     # Use cases + DTOs
├── adapters/        # Implementations của ports
├── delivery/        # Entry points (gRPC / lib)
├── shared/          # Logger, Tracing, Metrics, Result
└── infrastructure/  # Config, DI Container
```

**Luồng data:**
```
File → [Preprocessor] → [Parser] → [Chunker] → [Embedder] → [VectorStore + FileStorage]
```

---

## Trạng thái hiện tại

### DONE

#### domain/entities/ — Core data models
| File | Nội dung | Trạng thái |
|------|----------|-----------|
| `document.py` | `Document`, `DocumentType`, `ElementType`, `Section`, `ParsedDocument` | ✅ |
| `chunk.py` | `Chunk`, `ChunkMetadata` | ✅ |
| `embedding.py` | `Embedding` | ✅ |
| `search.py` | `SearchQuery`, `SearchFilter`, `SearchResult`, `SearchResponse` | ✅ |

#### domain/ports/ — Interfaces
| File | Interface | Trạng thái |
|------|-----------|-----------|
| `parser.py` | `IParser` → `Result[ParsedDocument]` | ✅ |
| `preprocessor.py` | `IPreprocessor` → `Path` | ✅ |
| `chunker.py` | `IChunker` → `Result[list[Chunk]]` | ✅ |
| `embedder.py` | `IEmbedder` → `Result[list[Embedding]]` | ✅ |
| `vector_store.py` | `IVectorStore` → `Result[...]` | ✅ |
| `file_storage.py` | `IFileStorage` → `str/None` | ✅ |
| `metadata_store.py` | `IMetadataStore` | ✅ |

#### shared/ — Cross-cutting concerns
| File | Nội dung | Trạng thái |
|------|----------|-----------|
| `result.py` | `Result[T, E]`, `Ok`, `Err` — Go-style error handling | ✅ |
| `logger.py` | `structlog` — structured logging, trace_id injection | ✅ |
| `tracing.py` | OpenTelemetry — traces → Jaeger/Tempo | ✅ |
| `metrics.py` | Prometheus — counters, histograms, gauges | ✅ |

#### infrastructure/
| File | Nội dung | Trạng thái |
|------|----------|-----------|
| `config.py` | `pydantic-settings` — load từ env/`.env` | ✅ |
| `container.py` | DI container — wire adapters + use cases | ✅ |

#### application/dto/
| File | DTOs | Trạng thái |
|------|------|-----------|
| `document_dto.py` | `ProcessDocumentRequest`, `ProcessDocumentResponse` | ✅ |
| `search_dto.py` | `SearchRequest`, `SearchResponse`, `SearchResultItem` | ✅ |

#### application/use_cases/
| File | Use case | Trạng thái |
|------|----------|-----------|
| `process_document.py` | Orchestrate: parse → chunk → embed → store | ✅ |
| `search_chunks.py` | Embed query → search → map DTOs | ✅ |

#### adapters/parsers/
| File | Nội dung | Trạng thái |
|------|----------|-----------|
| `pdf_parser.py` | PyMuPDF — font-based heading detection | ✅ tested |
| `docx_parser.py` | python-docx — Word styles → Section | ✅ tested |
| `pptx_parser.py` | python-pptx — slide → Section | ✅ |

#### adapters/chunkers/
| File | Nội dung | Trạng thái |
|------|----------|-----------|
| `heading_chunker.py` | Heading hierarchy + size constraints + overlap | ✅ tested |

#### adapters/embedders/
| File | Nội dung | Trạng thái |
|------|----------|-----------|
| `bge_embedder.py` | BGE-M3 via FlagEmbedding — lazy load, batch | ✅ |

---

### TODO — Còn phải làm

#### adapters/vector_db/ — DONE
| File | Nội dung | Trạng thái |
|------|----------|-----------|
| `qdrant_adapter.py` | Qdrant — upsert, search, delete, filter, auto-create collection | ✅ |

#### adapters/storage/ — DONE
| File | Nội dung | Trạng thái |
|------|----------|-----------|
| `minio_adapter.py` | MinIO — upload, download, delete, exists, presigned URL | ✅ |

#### adapters/ — Chưa implement
| File | Nội dung | Ưu tiên |
|------|----------|---------|
| `adapters/preprocessors/ocr_processor.py` | OCR cho scanned PDF | 🟡 MEDIUM |
| `adapters/embedders/openai_embedder.py` | OpenAI embeddings fallback | 🟢 LOW |

#### delivery/grpc/ — DONE
| File | Nội dung | Trạng thái |
|------|----------|-----------|
| `proto/chunking.proto` | 4 RPCs: ProcessDocument, Search, DeleteDocument, HealthCheck | ✅ |
| `proto/chunking_pb2.py` | Generated protobuf stubs | ✅ |
| `proto/chunking_pb2_grpc.py` | Generated gRPC stubs | ✅ |
| `servicer.py` | ChunkingServicer — bridge gRPC ↔ use cases | ✅ |
| `server.py` | gRPC server — ThreadPoolExecutor, graceful shutdown, 100MB limit | ✅ |

#### delivery/ — Chưa implement
| File | Nội dung | Ưu tiên |
|------|----------|---------|
| `delivery/lib/api.py` | Python library API (nếu dùng như package) | 🟢 LOW |

#### domain/exceptions/ — DONE
| File | Nội dung | Trạng thái |
|------|----------|-----------|
| `__init__.py` | `ChunkingError`, `ParseError`, `UnsupportedFileTypeError`, `ChunkError`, `EmbedError`, `VectorStoreError`, `FileStorageError`, `ProcessingError` | ✅ |

#### tests/ — Chưa có test nào
| Thư mục | Nội dung | Ưu tiên |
|---------|----------|---------|
| `tests/unit/domain/` | Test entities validation | 🟡 MEDIUM |
| `tests/unit/adapters/` | Test parsers, chunker với mock data | 🔴 HIGH |
| `tests/integration/` | Test end-to-end với Qdrant + MinIO thật | 🟡 MEDIUM |

---

## Thứ tự làm tiếp

```
✅ adapters/vector_db/qdrant_adapter.py
✅ adapters/storage/minio_adapter.py
✅ delivery/grpc/proto/chunking.proto
✅ delivery/grpc/server.py + servicer.py
1. domain/exceptions.py                    ← error types rõ ràng hơn
5. domain/exceptions.py                    ← error types rõ ràng hơn
6. tests/unit/adapters/                    ← test parsers + chunker
7. adapters/preprocessors/ocr_processor.py ← OCR cho scanned PDF
```

---

## Dependency map

```
delivery  →  application/use_cases  →  domain/ports  ←  adapters
                    ↓                        ↑
               application/dto          infrastructure/container (wire)
                    ↓
               domain/entities  ←  shared (Result, Logger, ...)
```

**Quy tắc dependency:**
- `domain/` không import gì từ `adapters/`, `infrastructure/`, `delivery/`
- `application/` chỉ import từ `domain/` và `shared/`
- `adapters/` implement `domain/ports/`
- `infrastructure/container.py` là nơi duy nhất biết về concrete implementations

---

## Tooling

| Tool | Version | Mục đích |
|------|---------|----------|
| `uv` | 0.10.10 | Dependency management, virtualenv |
| Python | 3.10.20 | Runtime |
| `structlog` | 25.5.0 | Structured logging |
| `opentelemetry-sdk` | 1.40.0 | Distributed tracing |
| `prometheus-client` | 0.24.1 | Metrics |
| `pydantic-settings` | 2.13.1 | Config management |
| `PyMuPDF` | ≥1.24.0 | PDF parsing |
| `python-docx` | ≥1.1.0 | DOCX parsing |
| `python-pptx` | ≥0.6.23 | PPTX parsing |
| `qdrant-client` | ≥1.9.0 | Vector DB |
| `minio` | ≥7.2.0 | Object storage |
| `FlagEmbedding` | optional | BGE-M3 embeddings |

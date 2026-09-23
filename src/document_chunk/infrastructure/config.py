from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Sub-configs — mỗi nhóm tương ứng một service/module
# ---------------------------------------------------------------------------

class AppConfig(BaseSettings):
    env: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_logs: bool = False  # True trong production để output JSON

    model_config = SettingsConfigDict(env_prefix="APP_")


class GrpcConfig(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 50051
    max_workers: int = 10

    model_config = SettingsConfigDict(env_prefix="GRPC_")


class HttpConfig(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000

    model_config = SettingsConfigDict(env_prefix="HTTP_")


class McpConfig(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8001
    path: str = "/mcp"

    model_config = SettingsConfigDict(env_prefix="MCP_")


class QdrantConfig(BaseSettings):
    host: str = "localhost"
    port: int = 6333
    api_key: str | None = None
    collection_name: str = "documents"
    vector_size: int = 1024  # BGE-M3 default

    model_config = SettingsConfigDict(env_prefix="QDRANT_")


class MinioConfig(BaseSettings):
    endpoint: str = "localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket_name: str = "documents"
    secure: bool = False  # True nếu dùng HTTPS

    model_config = SettingsConfigDict(env_prefix="MINIO_")


class SqlConfig(BaseSettings):
    enabled: bool = False
    dsn: str = "postgresql://postgres:postgres@localhost:5432/chunking"
    pool_size: int = 5
    connect_timeout_seconds: int = 10

    model_config = SettingsConfigDict(env_prefix="SQL_")


class Neo4jConfig(BaseSettings):
    enabled: bool = False
    uri: str = "bolt://localhost:7687"
    username: str = "neo4j"
    password: str = "neo4j"
    database: str = "neo4j"

    model_config = SettingsConfigDict(env_prefix="NEO4J_")


class RedisConfig(BaseSettings):
    host: str = "localhost"
    port: int = 6379
    password: str | None = None
    db: int = 0

    model_config = SettingsConfigDict(env_prefix="REDIS_")


class ParserConfig(BaseSettings):
    # PDF
    pdf_dpi: int = 150
    pdf_max_pages: int | None = None  # None = không giới hạn

    # PDF — ngân sách xử lý cho MỘT tài liệu.
    # concurrency=1 chỉ chặn số tài liệu song song, không chặn chi phí một file.
    pdf_max_file_bytes: int | None = 256 * 1024 * 1024   # None = không giới hạn
    pdf_timeout_seconds: float | None = 600.0            # None = không giới hạn
    pdf_extract_images: bool = False                      # bytes ảnh: chưa có consumer
    pdf_max_image_bytes: int = 64 * 1024 * 1024          # chỉ áp khi extract_images=True

    # PDF — ngưỡng đánh giá từng trang (xem pdf_page_assessment.py)
    pdf_min_page_chars: int = 40          # dưới mức này → trang bị coi là thiếu text
    pdf_scan_image_coverage: float = 0.5  # độ phủ ảnh ≥ mức này + ít text → trang scan
    pdf_detect_tables: bool = True        # tìm bảng để route sang layout backend

    # PDF — chính sách fallback và độ đầy đủ
    pdf_page_fallback_enabled: bool = True    # chạy OCR/layout backend cho trang thiếu
    pdf_full_fallback_page_ratio: float = 0.5  # ≥ tỉ lệ trang cần fallback → chạy cả file
    pdf_max_fallback_ranges: int = 8           # số lần gọi layout backend tối đa
    pdf_strict_missing_text: bool = True       # còn trang chưa ai đọc → Err, không Ok

    # PPTX
    pptx_include_notes: bool = True   # include speaker notes vào content

    # OCR
    ocr_enabled: bool = True
    ocr_languages: list[str] = Field(default=["vi", "en"])

    # Docling PDF parser
    docling_do_table_structure: bool = True
    docling_do_ocr: bool = True              # auto-detect: True + force_full_page_ocr=False
    docling_do_picture_description: bool = False   # requires VLM; off by default
    docling_picture_description_backend: str = "granite"  # or "openai_api"

    model_config = SettingsConfigDict(env_prefix="PARSER_")


class ChunkerConfig(BaseSettings):
    strategy: Literal["heading", "structural"] = "heading"
    target_tokens: int = Field(default=400, ge=8)
    max_tokens: int = Field(default=512, ge=8)
    min_tokens: int = Field(default=64, ge=0)
    overlap_tokens: int = Field(default=48, ge=0)
    header_max_tokens: int = Field(default=64, ge=0)
    embed_max_tokens: int = Field(default=1024, ge=8)
    oversize_tolerance: float = Field(default=1.15, ge=1)
    tokenizer: str = "BAAI/bge-m3"
    tokenizer_path: str | None = None
    index_toc: bool = False
    merge_across_top_level: bool = False

    @model_validator(mode="after")
    def validate_budgets(self):
        if not self.min_tokens <= self.target_tokens <= self.max_tokens:
            raise ValueError("Require min_tokens <= target_tokens <= max_tokens")
        if self.max_tokens + self.header_max_tokens + self.overlap_tokens + 2 > self.embed_max_tokens:
            raise ValueError("Chunk, header, overlap and special tokens exceed embedding budget")
        return self

    max_chunk_size: int = 1500     # chars
    min_chunk_size: int = 100      # chars
    overlap_size: int = 200        # chars

    model_config = SettingsConfigDict(env_prefix="CHUNKER_")


class LlamaIndexChunkerConfig(BaseSettings):
    # SentenceSplitter / TokenTextSplitter
    # 375 tokens ≈ 1500 chars (tương đương ChunkerConfig.max_chunk_size để so sánh công bằng)
    chunk_size: int = 375
    chunk_overlap: int = 50        # ~200 chars, tương đương ChunkerConfig.overlap_size

    # SemanticSplitterNodeParser
    semantic_buffer_size: int = 1
    semantic_breakpoint_percentile: int = 95
    # Model nhỏ để chạy nhanh; đổi sang "BAAI/bge-m3" để khớp với embedder chính
    semantic_embed_model: str = "BAAI/bge-small-en-v1.5"

    model_config = SettingsConfigDict(env_prefix="LLAMAINDEX_CHUNKER_")


class EmbedderConfig(BaseSettings):
    provider: Literal["bge", "openai", "sentence_transformers", "grpc"] = "bge"

    # Remote embedding-service
    grpc_target: str = "embedding-service:50051"

    # BGE
    bge_model: str = "BAAI/bge-m3"
    bge_use_fp16: bool = True

    # Common
    # Chunks are capped around 1,500 characters. Smaller batches avoid CPU OOM
    # when the PDF parser and BGE model briefly coexist in the worker process.
    batch_size: int = 4
    max_length: int = 1024

    # OpenAI
    openai_api_key: str | None = None
    openai_model: str = "text-embedding-3-small"

    model_config = SettingsConfigDict(env_prefix="EMBEDDER_")


class LlmConfig(BaseSettings):
    provider: Literal["gemini", "openai-compatible", "deepseek"] = "gemini"
    model: str = "gemini-2.0-flash"
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = 0.2
    timeout_seconds: int = 60
    max_retries: int = 2
    max_section_chars: int = 6000

    model_config = SettingsConfigDict(env_prefix="LLM_")


class TracingConfig(BaseSettings):
    enabled: bool = False
    otlp_endpoint: str | None = None  # e.g. "http://localhost:4318/v1/traces"
    service_name: str = "document-chunk"

    model_config = SettingsConfigDict(env_prefix="TRACING_")


class MetricsConfig(BaseSettings):
    enabled: bool = True
    port: int = 9090

    model_config = SettingsConfigDict(env_prefix="METRICS_")


# ---------------------------------------------------------------------------
# Root settings — compose tất cả sub-configs
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    app: AppConfig = Field(default_factory=AppConfig)
    grpc: GrpcConfig = Field(default_factory=GrpcConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)
    minio: MinioConfig = Field(default_factory=MinioConfig)
    sql: SqlConfig = Field(default_factory=SqlConfig)
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    parser: ParserConfig = Field(default_factory=ParserConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    llamaindex_chunker: LlamaIndexChunkerConfig = Field(default_factory=LlamaIndexChunkerConfig)
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",  # QDRANT__HOST=... maps to settings.qdrant.host
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """
    Singleton settings instance — load một lần, cache lại.

    Usage:
        from document_chunk.infrastructure.config import get_settings
        settings = get_settings()
        print(settings.qdrant.host)
    """
    return Settings()

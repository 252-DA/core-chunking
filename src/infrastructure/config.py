from functools import lru_cache
from typing import Literal

from pydantic import Field
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


class ParserConfig(BaseSettings):
    # PDF
    pdf_dpi: int = 150
    pdf_max_pages: int | None = None  # None = không giới hạn

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
    max_chunk_size: int = 1500     # chars
    min_chunk_size: int = 100      # chars
    overlap_size: int = 200        # chars

    model_config = SettingsConfigDict(env_prefix="CHUNKER_")


class EmbedderConfig(BaseSettings):
    provider: Literal["bge", "openai", "sentence_transformers"] = "bge"

    # BGE
    bge_model: str = "BAAI/bge-m3"
    bge_use_fp16: bool = True

    # Common
    batch_size: int = 32
    max_length: int = 8192

    # OpenAI
    openai_api_key: str | None = None
    openai_model: str = "text-embedding-3-small"

    model_config = SettingsConfigDict(env_prefix="EMBEDDER_")


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
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)
    minio: MinioConfig = Field(default_factory=MinioConfig)
    parser: ParserConfig = Field(default_factory=ParserConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
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
        from src.infrastructure.config import get_settings
        settings = get_settings()
        print(settings.qdrant.host)
    """
    return Settings()

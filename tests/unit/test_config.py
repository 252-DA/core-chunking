"""
Tests for infrastructure/config.py — Settings and sub-configs.
"""
import os
from unittest import mock

import pytest

from src.infrastructure.config import (
    AppConfig,
    ChunkerConfig,
    EmbedderConfig,
    get_settings,
    GrpcConfig,
    HttpConfig,
    LlamaIndexChunkerConfig,
    LlmConfig,
    MetricsConfig,
    MinioConfig,
    Neo4jConfig,
    OutboxConfig,
    ParserConfig,
    QdrantConfig,
    RedisConfig,
    Settings,
    SqlConfig,
    TracingConfig,
)


class TestAppConfig:
    def test_defaults(self):
        cfg = AppConfig()
        assert cfg.env == "development"
        assert cfg.debug is False
        assert cfg.log_level == "INFO"
        assert cfg.json_logs is False

    def test_env_prefix(self):
        with mock.patch.dict(os.environ, {"APP_ENV": "production", "APP_DEBUG": "true"}):
            cfg = AppConfig()
            assert cfg.env == "production"
            assert cfg.debug is True


class TestGrpcConfig:
    def test_defaults(self):
        cfg = GrpcConfig()
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 50051
        assert cfg.max_workers == 10


class TestHttpConfig:
    def test_defaults(self):
        cfg = HttpConfig()
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 8000


class TestQdrantConfig:
    def test_defaults(self):
        cfg = QdrantConfig()
        assert cfg.host == "localhost"
        assert cfg.port == 6333
        assert cfg.collection_name == "documents"
        assert cfg.vector_size == 1024


class TestMinioConfig:
    def test_defaults(self):
        cfg = MinioConfig()
        assert cfg.endpoint == "localhost:9000"
        assert cfg.access_key == "minioadmin"
        assert cfg.bucket_name == "documents"


class TestSqlConfig:
    def test_defaults(self):
        cfg = SqlConfig()
        assert cfg.enabled is False
        assert cfg.pool_size == 5

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"SQL_ENABLED": "true"}):
            cfg = SqlConfig()
            assert cfg.enabled is True


class TestNeo4jConfig:
    def test_defaults(self):
        cfg = Neo4jConfig()
        assert cfg.enabled is False
        assert cfg.uri == "bolt://localhost:7687"
        assert cfg.username == "neo4j"


class TestRedisConfig:
    def test_defaults(self):
        cfg = RedisConfig()
        assert cfg.host == "localhost"
        assert cfg.port == 6379
        assert cfg.password is None
        assert cfg.db == 0


class TestOutboxConfig:
    def test_defaults(self):
        cfg = OutboxConfig()
        assert cfg.enabled is True
        assert cfg.poll_interval_seconds == 5
        assert cfg.batch_size == 100
        assert cfg.max_attempts == 10


class TestParserConfig:
    def test_defaults(self):
        cfg = ParserConfig()
        assert cfg.pdf_dpi == 150
        assert cfg.pdf_max_pages is None
        assert cfg.pptx_include_notes is True
        assert cfg.ocr_enabled is True
        assert cfg.ocr_languages == ["vi", "en"]


class TestChunkerConfig:
    def test_defaults(self):
        cfg = ChunkerConfig()
        assert cfg.max_chunk_size == 1500
        assert cfg.min_chunk_size == 100
        assert cfg.overlap_size == 200


class TestLlamaIndexChunkerConfig:
    def test_defaults(self):
        cfg = LlamaIndexChunkerConfig()
        assert cfg.chunk_size == 375
        assert cfg.chunk_overlap == 50
        assert cfg.semantic_buffer_size == 1
        assert cfg.semantic_breakpoint_percentile == 95


class TestEmbedderConfig:
    def test_defaults(self):
        cfg = EmbedderConfig()
        assert cfg.provider == "bge"
        assert cfg.bge_model == "BAAI/bge-m3"
        assert cfg.bge_use_fp16 is True
        assert cfg.batch_size == 32
        assert cfg.max_length == 8192


class TestLlmConfig:
    def test_defaults(self):
        cfg = LlmConfig()
        assert cfg.provider == "gemini"
        assert cfg.model == "gemini-2.0-flash"
        assert cfg.temperature == 0.2
        assert cfg.timeout_seconds == 60


class TestTracingConfig:
    def test_defaults(self):
        cfg = TracingConfig()
        assert cfg.enabled is False
        assert cfg.otlp_endpoint is None
        assert cfg.service_name == "document-chunk"


class TestMetricsConfig:
    def test_defaults(self):
        cfg = MetricsConfig()
        assert cfg.enabled is True
        assert cfg.port == 9090


class TestSettings:
    def test_default_creation(self):
        settings = Settings()
        assert settings.app.env == "development"
        assert settings.qdrant.host == "localhost"
        assert settings.chunker.max_chunk_size == 1500

    def test_nested_delimiter(self):
        with mock.patch.dict(os.environ, {"QDRANT__HOST": "qdrant.example.com"}):
            settings = Settings()
            assert settings.qdrant.host == "qdrant.example.com"

    def test_all_sub_configs_present(self):
        settings = Settings()
        assert settings.app is not None
        assert settings.grpc is not None
        assert settings.http is not None
        assert settings.qdrant is not None
        assert settings.minio is not None
        assert settings.sql is not None
        assert settings.neo4j is not None
        assert settings.redis is not None
        assert settings.outbox is not None
        assert settings.parser is not None
        assert settings.chunker is not None
        assert settings.llamaindex_chunker is not None
        assert settings.embedder is not None
        assert settings.llm is not None
        assert settings.tracing is not None
        assert settings.metrics is not None


class TestGetSettings:
    def test_returns_settings_instance(self):
        settings = get_settings()
        assert isinstance(settings, Settings)

    def test_is_singleton(self):
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

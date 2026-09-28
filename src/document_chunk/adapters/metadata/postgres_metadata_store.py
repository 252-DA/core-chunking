from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TypedDict, cast

from document_chunk.adapters.metadata.postgres_chunk_repository import (
    PostgresChunkRepository,
)
from document_chunk.adapters.metadata.postgres_curriculum_repository import (
    PostgresCurriculumRepository,
)
from document_chunk.adapters.metadata.postgres_document_placement_repository import (
    PostgresDocumentPlacementRepository,
)
from document_chunk.adapters.metadata.postgres_document_repository import (
    PostgresDocumentRepository,
)
from document_chunk.adapters.metadata.postgres_enrichment_repository import (
    PostgresEnrichmentRepository,
)
from document_chunk.adapters.metadata.postgres_llm_usage_repository import (
    PostgresLlmUsageRepository,
)
from document_chunk.adapters.metadata.postgres_outbox_repository import (
    PostgresOutboxRepository,
)
from document_chunk.domain.ports.metadata_store import IMetadataStore
from document_chunk.infrastructure.config import SqlConfig


class _ConnectKwargs(TypedDict):
    connect_timeout: int


class PostgresMetadataStore(
    PostgresDocumentRepository,
    PostgresChunkRepository,
    PostgresEnrichmentRepository,
    PostgresCurriculumRepository,
    PostgresDocumentPlacementRepository,
    PostgresOutboxRepository,
    PostgresLlmUsageRepository,
    IMetadataStore,
):
    """PostgreSQL metadata facade composed from domain-focused repositories."""

    def __init__(self, sql_config: SqlConfig) -> None:
        self._sql_config = sql_config
        self._pool: Any | None = None

    def close(self) -> None:
        pool = self._pool
        if pool is None:
            return

        pool.close()
        self._pool = None

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        pool = self._get_pool()
        if pool is not None:
            with pool.connection() as conn:
                yield conn
            return

        try:
            import psycopg
        except ImportError as exc:
            raise ImportError("psycopg not installed. Install dependency: psycopg[binary]") from exc

        conn = psycopg.connect(
            self._sql_config.dsn,
            **self._connect_kwargs(),
        )
        try:
            yield conn
        finally:
            conn.close()

    def _get_pool(self) -> Any | None:
        if self._pool is not None:
            return self._pool

        try:
            from psycopg_pool import ConnectionPool
        except ImportError:
            return None

        self._pool = ConnectionPool(
            conninfo=self._sql_config.dsn,
            min_size=1,
            max_size=max(1, self._sql_config.pool_size),
            kwargs=cast(dict[str, Any], self._connect_kwargs()),
            open=True,
        )
        return self._pool

    def _connect_kwargs(self) -> _ConnectKwargs:
        return {
            "connect_timeout": self._sql_config.connect_timeout_seconds,
        }

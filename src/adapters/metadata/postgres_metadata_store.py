import json
import uuid
from contextlib import contextmanager
from pathlib import Path

from src.domain.entities.document import Document, DocumentType
from src.domain.ports.metadata_store import (
    DocumentFilter,
    IMetadataStore,
    IngestionStatus,
    OutboxEvent,
    StoredChunkMetadata,
)
from src.infrastructure.config import OutboxConfig, SqlConfig
from src.shared.logger import get_logger
from src.shared.result import Err, Ok, Result

logger = get_logger(__name__)


class PostgresMetadataStore(IMetadataStore):
    def __init__(self, sql_config: SqlConfig, outbox_config: OutboxConfig) -> None:
        self._sql_config = sql_config
        self._outbox_config = outbox_config
        self._schema_initialized = False

    @contextmanager
    def _connection(self):
        try:
            import psycopg
        except ImportError as exc:
            raise ImportError(
                "psycopg not installed. Install dependency: psycopg[binary]"
            ) from exc

        conn = psycopg.connect(
            self._sql_config.dsn,
            connect_timeout=self._sql_config.connect_timeout_seconds,
        )
        try:
            if not self._schema_initialized:
                self._ensure_schema(conn)
                self._schema_initialized = True
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS documents_metadata (
                    document_id TEXT PRIMARY KEY,
                    document_name TEXT NOT NULL,
                    doc_type TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes BIGINT NOT NULL,
                    storage_key TEXT,
                    course_id TEXT,
                    owner_id TEXT,
                    language TEXT,
                    status TEXT NOT NULL,
                    error_msg TEXT,
                    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks_metadata (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    chunk_index INT NOT NULL,
                    heading_path TEXT[] NOT NULL DEFAULT '{}',
                    heading_level INT NOT NULL DEFAULT 0,
                    page_number INT,
                    content_length INT NOT NULL DEFAULT 0,
                    language TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chunks_metadata_document_id
                ON chunks_metadata (document_id);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_events (
                    id UUID PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    payload_json JSONB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INT NOT NULL DEFAULT 0,
                    error_msg TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbox_events_status_created
                ON outbox_events (status, created_at);
                """
            )
        conn.commit()

    def upsert_document(
        self,
        document: Document,
        metadata: dict[str, str],
        status: IngestionStatus = IngestionStatus.QUEUED,
    ) -> Result[None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO documents_metadata (
                            document_id,
                            document_name,
                            doc_type,
                            mime_type,
                            size_bytes,
                            storage_key,
                            course_id,
                            owner_id,
                            language,
                            status,
                            metadata_json,
                            updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
                        ON CONFLICT (document_id)
                        DO UPDATE SET
                            document_name = EXCLUDED.document_name,
                            doc_type = EXCLUDED.doc_type,
                            mime_type = EXCLUDED.mime_type,
                            size_bytes = EXCLUDED.size_bytes,
                            storage_key = EXCLUDED.storage_key,
                            course_id = EXCLUDED.course_id,
                            owner_id = EXCLUDED.owner_id,
                            language = EXCLUDED.language,
                            status = EXCLUDED.status,
                            metadata_json = EXCLUDED.metadata_json,
                            updated_at = NOW();
                        """,
                        (
                            document.id,
                            document.name,
                            document.doc_type.value,
                            document.mime_type,
                            document.size_bytes,
                            metadata.get("storage_key"),
                            metadata.get("course_id"),
                            metadata.get("owner_id"),
                            metadata.get("language"),
                            status.value,
                            json.dumps(metadata),
                        ),
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(exc)

    def update_document_status(
        self,
        document_id: str,
        status: IngestionStatus,
        error_msg: str | None = None,
    ) -> Result[None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE documents_metadata
                        SET status = %s,
                            error_msg = %s,
                            updated_at = NOW()
                        WHERE document_id = %s;
                        """,
                        (status.value, error_msg, document_id),
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(exc)

    def upsert_chunks(self, chunks: list[StoredChunkMetadata]) -> Result[None, Exception]:
        if not chunks:
            return Ok(None)

        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    rows = [
                        (
                            c.chunk_id,
                            c.document_id,
                            c.chunk_index,
                            list(c.heading_path),
                            c.heading_level,
                            c.page_number,
                            c.content_length,
                            c.language,
                        )
                        for c in chunks
                    ]
                    cur.executemany(
                        """
                        INSERT INTO chunks_metadata (
                            chunk_id,
                            document_id,
                            chunk_index,
                            heading_path,
                            heading_level,
                            page_number,
                            content_length,
                            language,
                            updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (chunk_id)
                        DO UPDATE SET
                            document_id = EXCLUDED.document_id,
                            chunk_index = EXCLUDED.chunk_index,
                            heading_path = EXCLUDED.heading_path,
                            heading_level = EXCLUDED.heading_level,
                            page_number = EXCLUDED.page_number,
                            content_length = EXCLUDED.content_length,
                            language = EXCLUDED.language,
                            updated_at = NOW();
                        """,
                        rows,
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(exc)

    def append_outbox_event(
        self,
        event_type: str,
        aggregate_id: str,
        payload: dict,
    ) -> Result[str, Exception]:
        event_id = str(uuid.uuid4())
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO outbox_events (
                            id,
                            event_type,
                            aggregate_id,
                            payload_json,
                            status,
                            attempts,
                            error_msg,
                            updated_at
                        )
                        VALUES (%s, %s, %s, %s::jsonb, 'PENDING', 0, NULL, NOW());
                        """,
                        (event_id, event_type, aggregate_id, json.dumps(payload)),
                    )
                conn.commit()
            return Ok(event_id)
        except Exception as exc:
            return Err(exc)

    def fetch_pending_outbox(self, limit: int = 100) -> Result[list[OutboxEvent], Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id::text,
                               event_type,
                               aggregate_id,
                               payload_json::text,
                               status,
                               attempts,
                               error_msg,
                               created_at,
                               updated_at
                        FROM outbox_events
                        WHERE status = 'PENDING'
                        ORDER BY created_at ASC
                        LIMIT %s;
                        """,
                        (limit,),
                    )
                    rows = cur.fetchall()

            events = [
                OutboxEvent(
                    id=row[0],
                    event_type=row[1],
                    aggregate_id=row[2],
                    payload=json.loads(row[3]),
                    status=row[4],
                    attempts=row[5],
                    error_msg=row[6],
                    created_at=row[7],
                    updated_at=row[8],
                )
                for row in rows
            ]
            return Ok(events)
        except Exception as exc:
            return Err(exc)

    def mark_outbox_done(self, event_id: str) -> Result[None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE outbox_events
                        SET status = 'DONE',
                            updated_at = NOW()
                        WHERE id = %s::uuid;
                        """,
                        (event_id,),
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(exc)

    def mark_outbox_failed(self, event_id: str, error_msg: str) -> Result[None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE outbox_events
                        SET attempts = attempts + 1,
                            error_msg = %s,
                            status = CASE
                                WHEN attempts + 1 >= %s THEN 'FAILED'
                                ELSE 'PENDING'
                            END,
                            updated_at = NOW()
                        WHERE id = %s::uuid;
                        """,
                        (error_msg, self._outbox_config.max_attempts, event_id),
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(exc)

    def get(self, document_id: str) -> Result[Document | None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT document_id,
                               document_name,
                               doc_type,
                               mime_type,
                               size_bytes,
                               created_at
                        FROM documents_metadata
                        WHERE document_id = %s;
                        """,
                        (document_id,),
                    )
                    row = cur.fetchone()

            if row is None:
                return Ok(None)

            return Ok(self._row_to_document(row))
        except Exception as exc:
            return Err(exc)

    def list(
        self,
        filters: DocumentFilter | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Result[list[Document], Exception]:
        try:
            query = """
                SELECT document_id,
                       document_name,
                       doc_type,
                       mime_type,
                       size_bytes,
                       created_at
                FROM documents_metadata
            """
            where_clauses: list[str] = []
            params: list = []

            if filters:
                if filters.doc_types:
                    where_clauses.append("doc_type = ANY(%s)")
                    params.append([dt.value for dt in filters.doc_types])
                if filters.language:
                    where_clauses.append("language = %s")
                    params.append(filters.language)
                if filters.uploaded_after:
                    where_clauses.append("created_at >= %s")
                    params.append(filters.uploaded_after)
                if filters.uploaded_before:
                    where_clauses.append("created_at <= %s")
                    params.append(filters.uploaded_before)

            if where_clauses:
                query += " WHERE " + " AND ".join(where_clauses)

            query += " ORDER BY created_at DESC LIMIT %s OFFSET %s;"
            params.extend([limit, offset])

            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, tuple(params))
                    rows = cur.fetchall()

            docs = [self._row_to_document(row) for row in rows]
            return Ok(docs)
        except Exception as exc:
            return Err(exc)

    def delete(self, document_id: str) -> Result[None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM chunks_metadata WHERE document_id = %s;", (document_id,))
                    cur.execute("DELETE FROM outbox_events WHERE aggregate_id = %s;", (document_id,))
                    cur.execute(
                        "DELETE FROM documents_metadata WHERE document_id = %s;",
                        (document_id,),
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(exc)

    def _row_to_document(self, row: tuple) -> Document:
        document_id = row[0]
        document_name = row[1]
        return Document(
            id=document_id,
            name=document_name,
            path=Path(f"/virtual/{document_id}/{document_name}"),
            doc_type=DocumentType(row[2]),
            size_bytes=row[4],
            mime_type=row[3],
            created_at=row[5],
        )

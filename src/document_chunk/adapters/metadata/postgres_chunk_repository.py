from __future__ import annotations

import uuid
from typing import Any

from document_chunk.adapters.metadata.postgres_repository_utils import (
    PostgresRepositoryBase,
)
from document_chunk.domain.exceptions import DocumentStaleError, MetadataStoreError
from document_chunk.domain.outbox_events import OutboxEventType
from document_chunk.domain.ports.metadata_store import StoredChunkMetadata
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

tracer = get_tracer(__name__)


class PostgresChunkRepository(PostgresRepositoryBase):
    """Chunk persistence and chunk-query operations."""

    def upsert_chunks(self, chunks: list[StoredChunkMetadata]) -> Result[None, Exception]:
        if not chunks:
            return Ok(None)

        with tracer.start_as_current_span("postgres.upsert_chunks") as span:
            span.set_attribute("chunks.count", len(chunks))
            try:
                with self._connection() as conn:
                    with conn.cursor() as cur:
                        self._upsert_chunks_cursor(cur, chunks)
                    conn.commit()
                return Ok(None)
            except Exception as exc:
                return Err(
                    MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc)
                )

    def upsert_chunks_with_outbox(
        self,
        chunks: list[StoredChunkMetadata],
        event_type: OutboxEventType,
        aggregate_id: str,
        payload: dict,
    ) -> Result[str, Exception]:
        event_id = str(uuid.uuid4())
        with tracer.start_as_current_span("postgres.upsert_chunks_with_outbox") as span:
            span.set_attribute("chunks.count", len(chunks))
            span.set_attribute("event_type", OutboxEventType.normalize(event_type).value)
            span.set_attribute("aggregate_id", aggregate_id)
            try:
                with self._connection() as conn:
                    with conn.cursor() as cur:
                        document_id = chunks[0].document_id if chunks else aggregate_id
                        # Delete-race guard: chỉ ghi chunks + outbox khi document
                        # còn tồn tại và chưa bị soft-delete.
                        cur.execute(
                            """
                            SELECT EXISTS(
                                SELECT 1
                                FROM documents
                                WHERE document_id = %s::uuid
                                  AND deleted_at IS NULL
                            );
                            """,
                            (document_id,),
                        )
                        row = cur.fetchone()
                        active = bool(row and row[0])
                        if not active:
                            return Err(
                                DocumentStaleError(
                                    f"document {document_id} not found or deleted",
                                    document_id=document_id,
                                )
                            )
                        self._upsert_chunks_cursor(cur, chunks)
                        self._append_outbox_event_cursor(
                            cur=cur,
                            event_id=event_id,
                            event_type=event_type,
                            aggregate_id=aggregate_id,
                            payload=payload,
                        )
                    conn.commit()
                return Ok(event_id)
            except Exception as exc:
                return Err(
                    MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc)
                )

    def list_chunks(self, document_id: str) -> Result[list[StoredChunkMetadata], Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT chunk_id,
                               document_id,
                               sort_order,
                               heading_path,
                               0 AS heading_level,
                               page_number,
                               length(content) AS content_length,
                               language,
                               content,
                               NULL AS embedding_input
                        FROM chunks
                        WHERE document_id = %s::uuid
                          AND deleted_at IS NULL
                        ORDER BY sort_order ASC;
                        """,
                        (document_id,),
                    )
                    rows = cur.fetchall()

            return Ok(
                [
                    StoredChunkMetadata(
                        chunk_id=row[0],
                        document_id=row[1],
                        chunk_index=row[2],
                        heading_path=tuple(row[3] or ()),
                        heading_level=row[4],
                        page_number=row[5],
                        content_length=row[6],
                        language=row[7],
                        content_text=row[8],
                        embedding_input=row[9],
                    )
                    for row in rows
                ]
            )
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def _upsert_chunks_cursor(
        self,
        cur: Any,
        chunks: "list[StoredChunkMetadata]",
    ) -> None:
        rows = [
            (
                c.chunk_id,
                c.document_id,
                c.content_text or c.embedding_input or "",
                c.chunk_index,
                list(c.heading_path),
                c.page_number,
                c.document_id,
                c.language if c.language in {"vi", "en", "mixed"} else "vi",
            )
            for c in chunks
        ]
        if not rows:
            return

        cur.executemany(
            """
            INSERT INTO chunks (
                chunk_id,
                document_id,
                course_id,
                content,
                heading_path,
                page_number,
                sort_order,
                language
            )
            SELECT %s::uuid, %s::uuid, d.course_id, %s, %s, %s, %s, %s
            FROM documents d
            WHERE d.document_id = %s::uuid
              AND d.deleted_at IS NULL
            ON CONFLICT (chunk_id)
            DO UPDATE SET
                document_id = EXCLUDED.document_id,
                course_id = EXCLUDED.course_id,
                content = EXCLUDED.content,
                heading_path = EXCLUDED.heading_path,
                page_number = EXCLUDED.page_number,
                sort_order = EXCLUDED.sort_order,
                language = EXCLUDED.language;
            """,
            rows,
        )

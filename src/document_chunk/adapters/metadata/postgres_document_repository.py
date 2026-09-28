from __future__ import annotations

import builtins
import uuid

from document_chunk.adapters.metadata.postgres_repository_utils import (
    PostgresRepositoryBase,
)
from document_chunk.domain.entities.document import Document, DocumentType
from document_chunk.domain.exceptions import DocumentStaleError, MetadataStoreError
from document_chunk.domain.outbox_events import OutboxEventType
from document_chunk.domain.ports.metadata_store import (
    DocumentFilter,
    DocumentSummary,
    IngestionStatus,
    StoredDocumentContext,
)
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

tracer = get_tracer(__name__)
logger = get_logger(__name__)


class PostgresDocumentRepository(PostgresRepositoryBase):
    """Document lifecycle and document-query persistence operations."""

    def upsert_document(
        self,
        document: Document,
        metadata: dict[str, str],
        status: IngestionStatus = IngestionStatus.QUEUED,
    ) -> Result[None, Exception]:
        with tracer.start_as_current_span("postgres.upsert_document") as span:
            span.set_attribute("document.id", document.id)
            span.set_attribute("doc_type", document.doc_type.value)
            try:
                with self._connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO documents (
                                document_id,
                                course_id,
                                title,
                                file_path,
                                mime_type,
                                checksum,
                                status,
                                created_by
                            )
                            VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s::uuid)
                            ON CONFLICT (document_id)
                            DO UPDATE SET
                                course_id = EXCLUDED.course_id,
                                title = EXCLUDED.title,
                                file_path = EXCLUDED.file_path,
                                mime_type = EXCLUDED.mime_type,
                                checksum = EXCLUDED.checksum,
                                status = EXCLUDED.status,
                                created_by = COALESCE(EXCLUDED.created_by, documents.created_by),
                                deleted_at = NULL;
                            """,
                            (
                                document.id,
                                metadata.get("course_id"),
                                document.name,
                                metadata.get("storage_key") or str(document.path or document.name),
                                document.mime_type,
                                metadata.get("checksum"),
                                status.value,
                                metadata.get("owner_id"),
                            ),
                        )
                    conn.commit()
                return Ok(None)
            except Exception as exc:
                return Err(
                    MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc)
                )

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
                        UPDATE documents
                        SET status = %s
                        WHERE document_id = %s::uuid
                          AND deleted_at IS NULL;
                        """,
                        (status.value, document_id),
                    )
                    affected = cur.rowcount
                if affected == 0:
                    # Document missing or soft-deleted → worker must stop and
                    # must NOT write derived data (delete race guard).
                    return Err(
                        DocumentStaleError(
                            f"document {document_id} not found or deleted",
                            document_id=document_id,
                        )
                    )
                conn.commit()
            if error_msg:
                # documents table has no error column in this phase; log the
                # error so it is not silently dropped (see plan Phase 0).
                logger.error(
                    "document.status.error",
                    document_id=document_id,
                    status=status.value,
                    error=error_msg,
                )
            return Ok(None)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def get_document_context(
        self,
        document_id: str,
    ) -> Result[StoredDocumentContext | None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        -- ::text: StoredDocumentContext khai báo str; để psycopg trả
                        -- uuid.UUID thì các truy vấn sau so với cột varchar
                        -- (courses.code) vỡ "character varying = uuid".
                        SELECT document_id::text,
                               course_id::text,
                               created_by::text,
                               'vi' AS language
                        FROM documents
                        WHERE document_id = %s::uuid
                          AND deleted_at IS NULL;
                        """,
                        (document_id,),
                    )
                    row = cur.fetchone()

            if row is None:
                return Ok(None)

            return Ok(
                StoredDocumentContext(
                    document_id=row[0],
                    course_id=row[1],
                    owner_id=row[2],
                    language=row[3],
                )
            )
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def get(self, document_id: str) -> Result[Document | None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT document_id,
                               title,
                               mime_type,
                               file_path,
                               created_at
                        FROM documents
                        WHERE document_id = %s::uuid
                          AND deleted_at IS NULL;
                        """,
                        (document_id,),
                    )
                    row = cur.fetchone()

            if row is None:
                return Ok(None)

            return Ok(self._row_to_document(row))
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def list(
        self,
        filters: DocumentFilter | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Result[list[Document], Exception]:
        try:
            query = """
                SELECT document_id,
                       title,
                       mime_type,
                       file_path,
                       created_at
                FROM documents
            """
            where_clauses: list[str] = ["deleted_at IS NULL"]
            params: list = []

            if filters:
                if filters.doc_types:
                    where_clauses.append("mime_type = ANY(%s)")
                    params.append([self._mime_for_doc_type(dt) for dt in filters.doc_types])
                if filters.language:
                    where_clauses.append("%s = %s")
                    params.append(filters.language)
                    params.append(filters.language)
                    params.append(filters.language)
                if filters.uploaded_after:
                    where_clauses.append("created_at >= %s")
                    params.append(filters.uploaded_after)
                if filters.uploaded_before:
                    where_clauses.append("created_at <= %s")
                    params.append(filters.uploaded_before)

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
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def list_documents(
        self,
        course_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Result[builtins.list[DocumentSummary], Exception]:
        """List document summaries with chunk count, optional filter by course_id."""
        try:
            where_clause = ""
            params: builtins.list = [limit, offset]
            if course_id is not None:
                where_clause = "WHERE d.course_id = %s::uuid AND d.deleted_at IS NULL"
                params = [course_id, limit, offset]
            else:
                where_clause = "WHERE d.deleted_at IS NULL"
            query = f"""
                SELECT d.document_id,
                       d.title,
                       d.mime_type,
                       d.status,
                       d.course_id,
                       d.created_at,
                       (SELECT count(*) FROM chunks c WHERE c.document_id = d.document_id AND c.deleted_at IS NULL) AS chunk_count
                FROM documents d
                {where_clause}
                ORDER BY d.created_at DESC
                LIMIT %s OFFSET %s
            """
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, tuple(params))
                    rows = cur.fetchall()
            summaries = [
                DocumentSummary(
                    document_id=row[0],
                    document_name=row[1],
                    doc_type=row[2],
                    status=row[3],
                    course_id=row[4],
                    created_at=row[5],
                    chunk_count=row[6],
                )
                for row in rows
            ]
            return Ok(summaries)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def delete(self, document_id: str) -> Result[None, Exception]:
        event_id = str(uuid.uuid4())
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    # Cancel obsolete pending projections for the deleted aggregate.
                    cur.execute(
                        """
                        DELETE FROM outbox_events
                        WHERE aggregate_id = %s::uuid
                          AND status = 'PENDING';
                        """,
                        (document_id,),
                    )
                    self._append_outbox_event_cursor(
                        cur=cur,
                        event_id=event_id,
                        event_type=OutboxEventType.DOCUMENT_DELETED,
                        aggregate_id=document_id,
                        payload={"document_id": document_id},
                    )
                    cur.execute(
                        "UPDATE chunks SET deleted_at = NOW() WHERE document_id = %s::uuid;",
                        (document_id,),
                    )
                    cur.execute(
                        "UPDATE documents SET deleted_at = NOW() WHERE document_id = %s::uuid;",
                        (document_id,),
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def get_document_status(
        self, document_id: str
    ) -> Result[tuple[IngestionStatus, str | None, str | None] | None, Exception]:
        with tracer.start_as_current_span("postgres.get_document_status") as span:
            span.set_attribute("document.id", document_id)
            try:
                with self._connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT status, NULL AS error_msg, file_path
                            FROM documents
                            WHERE document_id = %s::uuid
                              AND deleted_at IS NULL;
                            """,
                            (document_id,),
                        )
                        row = cur.fetchone()
                if row is None:
                    return Ok(None)
                return Ok((IngestionStatus(row[0]), row[1], row[2]))
            except Exception as exc:
                return Err(
                    MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc)
                )

    # ------------------------------------------------------------------
    # Curriculum methods
    # ------------------------------------------------------------------

    def _row_to_document(self, row: tuple) -> Document:
        doc_type = self._doc_type_from_mime_or_path(row[2], row[3])
        return Document(
            id=row[0],
            name=row[1],
            path=None,
            doc_type=doc_type,
            size_bytes=0,
            mime_type=row[2] or self._mime_for_doc_type(doc_type),
            created_at=row[4],
        )

    def _doc_type_from_mime_or_path(self, mime_type: str | None, path: str | None) -> DocumentType:
        value = (mime_type or "").lower()
        suffix = (path or "").lower().rsplit(".", 1)[-1]
        if "pdf" in value or suffix == "pdf":
            return DocumentType.PDF
        if "word" in value or suffix == "docx":
            return DocumentType.DOCX
        if "presentation" in value or suffix == "pptx":
            return DocumentType.PPTX
        return DocumentType.MARKDOWN

    def _mime_for_doc_type(self, doc_type: DocumentType) -> str:
        return {
            DocumentType.PDF: "application/pdf",
            DocumentType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            DocumentType.PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            DocumentType.MARKDOWN: "text/markdown",
        }[doc_type]

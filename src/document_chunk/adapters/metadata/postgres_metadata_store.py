from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from threading import Lock

from document_chunk.domain.entities.document import Document, DocumentType
from document_chunk.domain.exceptions import MetadataStoreError
from document_chunk.domain.ports.metadata_store import (
    DocumentFilter,
    DocumentSummary,
    IMetadataStore,
    IngestionStatus,
    OutboxEvent,
    StoredAssessment,
    StoredChapter,
    StoredChunkConcept,
    StoredChunkLOMapping,
    StoredChunkMetadata,
    StoredConcept,
    StoredCourse,
    StoredDocumentContext,
    StoredLearningOutcome,
    StoredLessonCard,
    StoredQuizItem,
)
from document_chunk.infrastructure.config import OutboxConfig, SqlConfig
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_EVENT_DOCUMENT_DELETED = "document_deleted"


def _stable_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(value)))


class PostgresMetadataStore(IMetadataStore):
    def __init__(self, sql_config: SqlConfig, outbox_config: OutboxConfig) -> None:
        self._sql_config = sql_config
        self._outbox_config = outbox_config
        self._schema_initialized = False
        self._schema_init_lock = Lock()
        self._pool = None

    def close(self) -> None:
        pool = self.__dict__.get("_pool")
        if pool is None:
            return

        pool.close()
        self._pool = None

    @contextmanager
    def _connection(self):
        pool = self._get_pool()
        if pool is not None:
            with pool.connection() as conn:
                self._ensure_schema_initialized(conn)
                yield conn
            return

        try:
            import psycopg
        except ImportError as exc:
            raise ImportError(
                "psycopg not installed. Install dependency: psycopg[binary]"
            ) from exc

        conn = psycopg.connect(
            self._sql_config.dsn,
            **self._connect_kwargs(),
        )
        try:
            self._ensure_schema_initialized(conn)
            yield conn
        finally:
            conn.close()

    def _get_pool(self):
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
            kwargs=self._connect_kwargs(),
            open=True,
        )
        return self._pool

    def _connect_kwargs(self) -> dict:
        return {
            "connect_timeout": self._sql_config.connect_timeout_seconds,
        }

    def _ensure_schema_initialized(self, conn) -> None:
        if self._schema_initialized:
            return

        with self._schema_init_lock:
            if self._schema_initialized:
                return

            # Schema is owned by core-api/prisma/migrations/0_init/migration.sql.
            # This worker must not recreate the historical documents_metadata /
            # chunks_metadata / chunk_contents schema against the report DDL.
            self._schema_initialized = True

    def _ensure_schema(self, conn) -> None:
        return
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
                CREATE TABLE IF NOT EXISTS chunk_contents (
                    chunk_id TEXT PRIMARY KEY REFERENCES chunks_metadata (chunk_id) ON DELETE CASCADE,
                    document_id TEXT NOT NULL,
                    content_text TEXT NOT NULL DEFAULT '',
                    embedding_input TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chunk_contents_document_id
                ON chunk_contents (document_id);
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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS concepts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    domain TEXT,
                    category TEXT NOT NULL DEFAULT 'other',
                    language TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_concepts_slug
                ON concepts (slug);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chunk_concepts (
                    chunk_id TEXT NOT NULL REFERENCES chunks_metadata (chunk_id) ON DELETE CASCADE,
                    concept_id TEXT NOT NULL REFERENCES concepts (id) ON DELETE CASCADE,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                    source TEXT NOT NULL DEFAULT 'heading',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (chunk_id, concept_id)
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chunk_concepts_concept_id
                ON chunk_concepts (concept_id);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS lesson_cards (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents_metadata (document_id) ON DELETE CASCADE,
                    primary_chunk_id TEXT NOT NULL REFERENCES chunks_metadata (chunk_id) ON DELETE CASCADE,
                    source_chunk_ids TEXT[] NOT NULL DEFAULT '{}',
                    heading_path TEXT[] NOT NULL DEFAULT '{}',
                    title TEXT NOT NULL,
                    bullets TEXT[] NOT NULL DEFAULT '{}',
                    key_insight TEXT,
                    card_index INT NOT NULL DEFAULT 0,
                    model_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_lesson_cards_document_chunk_order
                ON lesson_cards (document_id, primary_chunk_id, card_index);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS quiz_items (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents_metadata (document_id) ON DELETE CASCADE,
                    primary_chunk_id TEXT NOT NULL REFERENCES chunks_metadata (chunk_id) ON DELETE CASCADE,
                    source_chunk_ids TEXT[] NOT NULL DEFAULT '{}',
                    heading_path TEXT[] NOT NULL DEFAULT '{}',
                    question TEXT NOT NULL,
                    choices TEXT[] NOT NULL DEFAULT '{}',
                    correct_index INT NOT NULL,
                    explanation TEXT,
                    difficulty TEXT NOT NULL DEFAULT 'medium',
                    question_index INT NOT NULL DEFAULT 0,
                    model_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_quiz_items_document_chunk_order
                ON quiz_items (document_id, primary_chunk_id, question_index);
                """
            )

            # ------------------------------------------------------------------
            # Curriculum-Aware layer tables
            # ------------------------------------------------------------------
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS courses (
                    course_id TEXT PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    title_vi TEXT NOT NULL,
                    title_en TEXT,
                    credits INT,
                    semester TEXT,
                    source_document_id TEXT REFERENCES documents_metadata(document_id) ON DELETE SET NULL,
                    extraction_confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chapters (
                    chapter_id TEXT PRIMARY KEY,
                    course_id TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    title TEXT NOT NULL,
                    order_index INT NOT NULL DEFAULT 0,
                    UNIQUE(course_id, code)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_outcomes (
                    lo_id TEXT PRIMARY KEY,
                    course_id TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    parent_code TEXT,
                    statement_vi TEXT NOT NULL,
                    statement_en TEXT,
                    bloom_level TEXT,
                    cdio_level INT,
                    UNIQUE(course_id, code)
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_lo_course ON learning_outcomes(course_id);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS assessments (
                    assessment_id TEXT PRIMARY KEY,
                    course_id TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    name_vi TEXT NOT NULL,
                    name_en TEXT,
                    category TEXT NOT NULL DEFAULT 'quiz',
                    weight DOUBLE PRECISION,
                    UNIQUE(course_id, code)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS lo_assessments (
                    lo_id TEXT REFERENCES learning_outcomes(lo_id) ON DELETE CASCADE,
                    assessment_id TEXT REFERENCES assessments(assessment_id) ON DELETE CASCADE,
                    PRIMARY KEY(lo_id, assessment_id)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chunk_lo_mappings (
                    chunk_id TEXT REFERENCES chunks_metadata(chunk_id) ON DELETE CASCADE,
                    lo_id TEXT REFERENCES learning_outcomes(lo_id) ON DELETE CASCADE,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY(chunk_id, lo_id)
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_clm_lo ON chunk_lo_mappings(lo_id);
                """
            )
            cur.execute("ALTER TABLE quiz_items ADD COLUMN IF NOT EXISTS lo_id TEXT REFERENCES learning_outcomes(lo_id) ON DELETE SET NULL;")
            cur.execute("ALTER TABLE quiz_items ADD COLUMN IF NOT EXISTS assessment_id TEXT REFERENCES assessments(assessment_id) ON DELETE SET NULL;")
            cur.execute("ALTER TABLE quiz_items ADD COLUMN IF NOT EXISTS bloom_level TEXT;")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_quiz_items_lo ON quiz_items(lo_id);")

            # ── LMS integration tables ──
            cur.execute(
                """
                DO $$ BEGIN
                  CREATE TYPE lms_type_enum AS ENUM ('openedx','moodle','canvas');
                EXCEPTION WHEN duplicate_object THEN null;
                END $$;
                """
            )
            cur.execute("ALTER TYPE lms_type_enum ADD VALUE IF NOT EXISTS 'canvas';")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS lms_user_mappings (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    lms_type lms_type_enum NOT NULL,
                    lms_user_id VARCHAR(255) NOT NULL,
                    internal_user_id UUID NOT NULL DEFAULT gen_random_uuid(),
                    email VARCHAR(255),
                    display_name VARCHAR(255),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(lms_type, lms_user_id)
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_lum_internal ON lms_user_mappings(internal_user_id);"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS lms_course_ref (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    lms_type lms_type_enum NOT NULL,
                    lms_course_id VARCHAR(255) NOT NULL,
                    course_id TEXT REFERENCES courses(course_id),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(lms_type, lms_course_id)
                );
                """
            )
        conn.commit()

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
                return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

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
                        SET status = %s,
                            deleted_at = NULL
                        WHERE document_id = %s::uuid;
                        """,
                        (status.value, document_id),
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

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
                return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def upsert_chunks_with_outbox(
        self,
        chunks: list[StoredChunkMetadata],
        event_type: str,
        aggregate_id: str,
        payload: dict,
    ) -> Result[str, Exception]:
        event_id = str(uuid.uuid4())
        with tracer.start_as_current_span("postgres.upsert_chunks_with_outbox") as span:
            span.set_attribute("chunks.count", len(chunks))
            span.set_attribute("event_type", event_type)
            span.set_attribute("aggregate_id", aggregate_id)
            try:
                with self._connection() as conn:
                    with conn.cursor() as cur:
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
                        SELECT document_id,
                               course_id,
                               created_by,
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

    def upsert_concepts(self, concepts: list[StoredConcept]) -> Result[None, Exception]:
        if not concepts:
            return Ok(None)

        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    self._upsert_concepts_cursor(cur, concepts)
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def upsert_chunk_concepts(
        self,
        chunk_concepts: list[StoredChunkConcept],
    ) -> Result[None, Exception]:
        if not chunk_concepts:
            return Ok(None)

        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    self._upsert_chunk_concepts_cursor(cur, chunk_concepts)
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def persist_enrichment_batch(
        self,
        document_id: str,
        lesson_cards: list[StoredLessonCard],
        quiz_items: list[StoredQuizItem],
        concepts: list[StoredConcept],
        chunk_concepts: list[StoredChunkConcept],
        outbox_event_type: str | None = None,
        outbox_payload: dict | None = None,
    ) -> Result[str | None, Exception]:
        event_id: str | None = None
        if outbox_event_type is not None:
            event_id = str(uuid.uuid4())
            if outbox_payload is None:
                return Err(
                    MetadataStoreError("outbox_payload is required when outbox_event_type is provided")
                )

        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    self._replace_lesson_cards_cursor(cur, document_id, lesson_cards)
                    self._replace_quiz_items_cursor(cur, document_id, quiz_items)
                    self._replace_chunk_concepts_cursor(cur, document_id, chunk_concepts)
                    self._upsert_concepts_cursor(cur, concepts)
                    if event_id is not None and outbox_payload is not None:
                        self._delete_pending_outbox_cursor(cur, document_id, outbox_event_type)
                        self._append_outbox_event_cursor(
                            cur=cur,
                            event_id=event_id,
                            event_type=outbox_event_type,
                            aggregate_id=document_id,
                            payload=outbox_payload,
                        )
                conn.commit()
            return Ok(event_id)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def list_lesson_cards(self, document_id: str) -> Result[list[StoredLessonCard], Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT lc.card_id::text,
                               c.document_id::text,
                               c.chunk_id::text,
                               lc.source_chunk_ids,
                               lc.title,
                               lc.content,
                               row_number() OVER (ORDER BY c.sort_order ASC, lc.created_at ASC) - 1 AS card_index
                        FROM lesson_cards lc
                        JOIN chunks c
                          ON c.chunk_id = ANY(lc.source_chunk_ids)
                        WHERE c.document_id = %s::uuid
                          AND lc.deleted_at IS NULL
                          AND c.deleted_at IS NULL
                        ORDER BY c.sort_order ASC, lc.created_at ASC, lc.card_id ASC;
                        """,
                        (document_id,),
                    )
                    rows = cur.fetchall()

            return Ok(
                [
                    StoredLessonCard(
                        card_id=row[0],
                        document_id=row[1],
                        primary_chunk_id=row[2],
                        source_chunk_ids=tuple(row[3] or ()),
                        heading_path=tuple((row[5] or {}).get("heading_path") or ()),
                        title=row[4],
                        bullets=tuple((row[5] or {}).get("bullets") or ()),
                        key_insight=(row[5] or {}).get("key_insight"),
                        card_index=row[6],
                        model_id=None,
                    )
                    for row in rows
                ]
            )
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def list_quiz_items(self, document_id: str) -> Result[list[StoredQuizItem], Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT qi.quiz_id::text,
                               c.document_id::text,
                               c.chunk_id::text,
                               qi.source_chunk_ids,
                               qi.question,
                               qi.options,
                               qi.correct_answer,
                               qi.explanation,
                               row_number() OVER (ORDER BY c.sort_order ASC, qi.created_at ASC) - 1 AS question_index
                        FROM quiz_items qi
                        JOIN chunks c
                          ON c.chunk_id = ANY(qi.source_chunk_ids)
                        WHERE c.document_id = %s::uuid
                          AND qi.deleted_at IS NULL
                          AND c.deleted_at IS NULL
                        ORDER BY c.sort_order ASC, qi.created_at ASC, qi.quiz_id ASC;
                        """,
                        (document_id,),
                    )
                    rows = cur.fetchall()

            return Ok(
                [
                    StoredQuizItem(
                        question_id=row[0],
                        document_id=row[1],
                        primary_chunk_id=row[2],
                        source_chunk_ids=tuple(row[3] or ()),
                        heading_path=tuple(),
                        question=row[4],
                        choices=tuple(row[5] or ()),
                        correct_index=(row[5] or []).index(row[6]) if row[6] in (row[5] or []) else 0,
                        explanation=row[7],
                        difficulty="medium",
                        question_index=row[8],
                        model_id=None,
                    )
                    for row in rows
                ]
            )
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

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
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def fetch_pending_outbox(self, limit: int = 100) -> Result[list[OutboxEvent], Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT event_id::text,
                               event_type,
                               aggregate_id::text,
                               payload::text,
                               status,
                               retry_count,
                               last_error,
                               occurred_at,
                               COALESCE(processed_at, occurred_at)
                        FROM outbox_events
                        WHERE status = 'PENDING'
                        ORDER BY occurred_at ASC
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
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def mark_outbox_done(self, event_id: str) -> Result[None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE outbox_events
                        SET status = 'PROCESSED',
                            processed_at = NOW()
                        WHERE event_id = %s::uuid;
                        """,
                        (event_id,),
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def mark_outbox_failed(self, event_id: str, error_msg: str) -> Result[None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE outbox_events
                        SET retry_count = retry_count + 1,
                            last_error = %s,
                            status = CASE
                                WHEN retry_count + 1 >= %s THEN 'FAILED'
                                ELSE 'PENDING'
                            END
                        WHERE event_id = %s::uuid;
                        """,
                        (error_msg, self._outbox_config.max_attempts, event_id),
                    )
                conn.commit()
            return Ok(None)
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
    ) -> Result[list[DocumentSummary], Exception]:
        """List document summaries with chunk count, optional filter by course_id."""
        try:
            where_clause = ""
            params: list = [limit, offset]
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
                    cur.execute(
                        """
                        INSERT INTO outbox_events (
                            event_id,
                            event_type,
                            aggregate_type,
                            aggregate_id,
                            payload,
                            status,
                            retry_count,
                            last_error
                        )
                        VALUES (%s::uuid, %s, 'document', %s::uuid, %s::jsonb, 'PENDING', 0, NULL);
                        """,
                        (
                            event_id,
                            "DOCUMENT_DELETED",
                            document_id,
                            json.dumps({"document_id": document_id}),
                        ),
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
                return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    # ------------------------------------------------------------------
    # Curriculum methods
    # ------------------------------------------------------------------

    def upsert_curriculum(
        self,
        course: StoredCourse,
        chapters: list[StoredChapter],
        learning_outcomes: list[StoredLearningOutcome],
        assessments: list[StoredAssessment],
        lo_assessment_links: list[tuple[str, str]],
    ) -> Result[None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO courses (
                            course_id, code, name, description, updated_at
                        )
                        VALUES (%s::uuid, %s, %s, %s, NOW())
                        ON CONFLICT (course_id) DO UPDATE SET
                            code = EXCLUDED.code,
                            name = EXCLUDED.name,
                            description = EXCLUDED.description,
                            updated_at = NOW();
                        """,
                        (
                            _stable_uuid(course.course_id),
                            course.code,
                            course.title_vi,
                            course.title_en or course.semester,
                        ),
                    )

                    if chapters:
                        cur.executemany(
                            """
                            INSERT INTO chapters (chapter_id, course_id, title, sort_order, deleted_at)
                            VALUES (%s::uuid, %s::uuid, %s, %s, NULL)
                            ON CONFLICT (chapter_id) DO UPDATE SET
                                title = EXCLUDED.title,
                                sort_order = EXCLUDED.sort_order,
                                deleted_at = NULL;
                            """,
                            [
                                (
                                    _stable_uuid(c.chapter_id),
                                    _stable_uuid(c.course_id),
                                    f"{c.code} {c.title}".strip(),
                                    c.order_index,
                                )
                                for c in chapters
                            ],
                        )

                    if learning_outcomes:
                        chapter_by_code = {c.code: c.chapter_id for c in chapters}
                        cur.executemany(
                            """
                            INSERT INTO learning_outcomes (
                                lo_id, chapter_id, code, statement_vi, statement_en,
                                bloom_level, cdio_level, academic_year, version, is_current, deleted_at
                            )
                            VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, 'default', 1, TRUE, NULL)
                            ON CONFLICT (chapter_id, code, academic_year, version) DO UPDATE SET
                                statement_vi = EXCLUDED.statement_vi,
                                statement_en = EXCLUDED.statement_en,
                                bloom_level = EXCLUDED.bloom_level,
                                cdio_level = EXCLUDED.cdio_level,
                                is_current = TRUE,
                                deleted_at = NULL;
                            """,
                            [
                                (
                                    _stable_uuid(lo.lo_id),
                                    _stable_uuid(
                                        chapter_by_code.get(
                                            self._chapter_code_from_lo(lo.code),
                                            f"{lo.course_id}:CH{self._chapter_code_from_lo(lo.code)}",
                                        )
                                    ),
                                    lo.code,
                                    lo.statement_vi,
                                    lo.statement_en,
                                    self._bloom_level(lo.bloom_level),
                                    self._cdio_level(lo.cdio_level),
                                )
                                for lo in learning_outcomes
                            ],
                        )

                    if assessments:
                        cur.executemany(
                            """
                            INSERT INTO assessments (
                                assessment_id, course_id, title, max_points, sort_order, type, deleted_at
                            )
                            VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, NULL)
                            ON CONFLICT (assessment_id) DO UPDATE SET
                                title = EXCLUDED.title,
                                max_points = EXCLUDED.max_points,
                                sort_order = EXCLUDED.sort_order,
                                type = EXCLUDED.type,
                                deleted_at = NULL;
                            """,
                            [
                                (
                                    _stable_uuid(a.assessment_id),
                                    _stable_uuid(a.course_id),
                                    a.name_vi,
                                    a.weight or 100,
                                    index + 1,
                                    "QUIZ" if a.category.lower() == "quiz" else "ASSIGNMENT",
                                )
                                for index, a in enumerate(assessments)
                            ],
                        )

                    if lo_assessment_links:
                        cur.executemany(
                            """
                            INSERT INTO lo_assessments (lo_id, assessment_id)
                            VALUES (%s::uuid, %s::uuid)
                            ON CONFLICT DO NOTHING;
                            """,
                            [(_stable_uuid(lo_id), _stable_uuid(assessment_id)) for lo_id, assessment_id in lo_assessment_links],
                        )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def get_curriculum(
        self, course_id: str
    ) -> Result[
        tuple[StoredCourse, list[StoredChapter], list[StoredLearningOutcome], list[StoredAssessment]] | None,
        Exception,
    ]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT course_id::text, code, name, description
                        FROM courses
                        WHERE course_id = %s::uuid
                          AND deleted_at IS NULL;
                        """,
                        (_stable_uuid(course_id),),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return Ok(None)
                    course = StoredCourse(
                        course_id=row[0], code=row[1], title_vi=row[2], title_en=row[3],
                    )

                    cur.execute(
                        """
                        SELECT chapter_id::text, course_id::text, title, sort_order
                        FROM chapters
                        WHERE course_id = %s::uuid
                          AND deleted_at IS NULL
                        ORDER BY sort_order;
                        """,
                        (_stable_uuid(course_id),),
                    )
                    chapters = [
                        StoredChapter(
                            chapter_id=r[0],
                            course_id=r[1],
                            code=str(r[3]),
                            title=r[2],
                            order_index=r[3],
                        )
                        for r in cur.fetchall()
                    ]

                    cur.execute(
                        """
                        SELECT lo.lo_id::text, ch.course_id::text, lo.code, NULL AS parent_code,
                               lo.statement_vi, lo.statement_en, lo.bloom_level, lo.cdio_level
                        FROM learning_outcomes lo
                        JOIN chapters ch ON ch.chapter_id = lo.chapter_id
                        WHERE ch.course_id = %s::uuid
                          AND lo.deleted_at IS NULL
                          AND lo.is_current = TRUE;
                        """,
                        (_stable_uuid(course_id),),
                    )
                    los = [
                        StoredLearningOutcome(
                            lo_id=r[0], course_id=r[1], code=r[2], parent_code=r[3],
                            statement_vi=r[4], statement_en=r[5], bloom_level=r[6], cdio_level=r[7],
                        )
                        for r in cur.fetchall()
                    ]

                    cur.execute(
                        """
                        SELECT assessment_id::text, course_id::text, title, type, max_points
                        FROM assessments
                        WHERE course_id = %s::uuid
                          AND deleted_at IS NULL;
                        """,
                        (_stable_uuid(course_id),),
                    )
                    assessments = [
                        StoredAssessment(
                            assessment_id=r[0], course_id=r[1], code=r[2], name_vi=r[2],
                            name_en=None, category=r[3], weight=float(r[4]),
                        )
                        for r in cur.fetchall()
                    ]
            return Ok((course, chapters, los, assessments))
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def list_los_by_chapter(
        self, course_id: str, chapter_code: str
    ) -> Result[list[StoredLearningOutcome], Exception]:
        try:
            prefix = f"L.O.{chapter_code}."
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT lo_id, course_id, code, parent_code, statement_vi,
                               statement_en, bloom_level, cdio_level
                        FROM (
                            SELECT lo.lo_id::text AS lo_id,
                                   ch.course_id::text AS course_id,
                                   lo.code,
                                   NULL AS parent_code,
                                   lo.statement_vi,
                                   lo.statement_en,
                                   lo.bloom_level,
                                   lo.cdio_level
                            FROM learning_outcomes lo
                            JOIN chapters ch ON ch.chapter_id = lo.chapter_id
                            WHERE ch.course_id = %s::uuid
                              AND lo.deleted_at IS NULL
                              AND lo.is_current = TRUE
                        ) q
                        WHERE code LIKE %s OR code = %s;
                        """,
                        (_stable_uuid(course_id), prefix + "%", f"L.O.{chapter_code}"),
                    )
                    return Ok([
                        StoredLearningOutcome(
                            lo_id=r[0], course_id=r[1], code=r[2], parent_code=r[3],
                            statement_vi=r[4], statement_en=r[5], bloom_level=r[6], cdio_level=r[7],
                        )
                        for r in cur.fetchall()
                    ])
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def list_los_by_assessment(
        self, course_id: str, assessment_code: str
    ) -> Result[list[StoredLearningOutcome], Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT lo.lo_id::text, ch.course_id::text, lo.code, NULL AS parent_code,
                               lo.statement_vi, lo.statement_en, lo.bloom_level, lo.cdio_level
                        FROM learning_outcomes lo
                        JOIN lo_assessments la ON la.lo_id = lo.lo_id
                        JOIN assessments a ON a.assessment_id = la.assessment_id
                        JOIN chapters ch ON ch.chapter_id = lo.chapter_id
                        WHERE ch.course_id = %s::uuid AND a.title = %s;
                        """,
                        (_stable_uuid(course_id), assessment_code),
                    )
                    return Ok([
                        StoredLearningOutcome(
                            lo_id=r[0], course_id=r[1], code=r[2], parent_code=r[3],
                            statement_vi=r[4], statement_en=r[5], bloom_level=r[6], cdio_level=r[7],
                        )
                        for r in cur.fetchall()
                    ])
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def upsert_chunk_lo_mappings(
        self,
        mappings: list[StoredChunkLOMapping],
    ) -> Result[None, Exception]:
        if not mappings:
            return Ok(None)
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO chunk_lo_mappings (chunk_id, lo_id, confidence)
                        VALUES (%s::uuid, %s::uuid, %s)
                        ON CONFLICT (chunk_id, lo_id) DO UPDATE SET
                            confidence = GREATEST(EXCLUDED.confidence, chunk_lo_mappings.confidence);
                        """,
                        [(m.chunk_id, m.lo_id, m.confidence) for m in mappings],
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def list_chunks_for_lo(
        self, lo_id: str
    ) -> Result[list[StoredChunkMetadata], Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT c.chunk_id, c.document_id, c.sort_order,
                               c.heading_path, 0 AS heading_level, c.page_number,
                               length(c.content) AS content_length, c.language,
                               c.content, NULL AS embedding_input
                        FROM chunk_lo_mappings clm
                        JOIN chunks c ON c.chunk_id = clm.chunk_id
                        WHERE clm.lo_id = %s::uuid
                          AND c.deleted_at IS NULL
                        ORDER BY clm.confidence DESC;
                        """,
                        (lo_id,),
                    )
                    return Ok([
                        StoredChunkMetadata(
                            chunk_id=r[0], document_id=r[1], chunk_index=r[2],
                            heading_path=tuple(r[3] or ()), heading_level=r[4],
                            page_number=r[5], content_length=r[6], language=r[7],
                            content_text=r[8], embedding_input=r[9],
                        )
                        for r in cur.fetchall()
                    ])
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

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

    def _bloom_level(self, value: str | int | None) -> int:
        if isinstance(value, int) and 1 <= value <= 6:
            return value
        mapping = {
            "remember": 1,
            "understand": 2,
            "apply": 3,
            "analyze": 4,
            "evaluate": 5,
            "create": 6,
        }
        return mapping.get(str(value or "").lower(), 2)

    def _cdio_level(self, value: str | int | None) -> str:
        if str(value) in {"I", "II", "III"}:
            return str(value)
        if value == 1:
            return "I"
        if value == 2:
            return "II"
        return "III"

    def _chapter_code_from_lo(self, code: str) -> str:
        parts = code.replace("L.O.", "").split(".")
        return parts[0] if parts and parts[0] else "1"

    def _upsert_chunks_cursor(self, cur, chunks: "list[StoredChunkMetadata]") -> None:
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
            ON CONFLICT (chunk_id)
            DO UPDATE SET
                document_id = EXCLUDED.document_id,
                course_id = EXCLUDED.course_id,
                content = EXCLUDED.content,
                heading_path = EXCLUDED.heading_path,
                page_number = EXCLUDED.page_number,
                sort_order = EXCLUDED.sort_order,
                language = EXCLUDED.language,
                deleted_at = NULL;
            """,
            rows,
        )

    def _upsert_concepts_cursor(self, cur, concepts: "list[StoredConcept]") -> None:
        if not concepts:
            return

        cur.executemany(
            """
            INSERT INTO concepts (
                id,
                name,
                canonical_name,
                slug,
                domain,
                category,
                language,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (id)
            DO UPDATE SET
                name = EXCLUDED.name,
                canonical_name = EXCLUDED.canonical_name,
                slug = EXCLUDED.slug,
                domain = EXCLUDED.domain,
                category = EXCLUDED.category,
                language = COALESCE(EXCLUDED.language, concepts.language),
                updated_at = NOW();
            """,
            [
                (
                    concept.concept_id,
                    concept.name,
                    concept.canonical_name,
                    concept.slug,
                    concept.domain,
                    concept.category,
                    concept.language,
                )
                for concept in concepts
            ],
        )

    def _upsert_chunk_concepts_cursor(self, cur, chunk_concepts: "list[StoredChunkConcept]") -> None:
        if not chunk_concepts:
            return

        cur.executemany(
            """
            INSERT INTO chunk_concepts (
                chunk_id,
                concept_id,
                confidence,
                source,
                updated_at
            )
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (chunk_id, concept_id)
            DO UPDATE SET
                confidence = EXCLUDED.confidence,
                source = EXCLUDED.source,
                updated_at = NOW();
            """,
            [
                (
                    chunk_concept.chunk_id,
                    chunk_concept.concept_id,
                    chunk_concept.confidence,
                    chunk_concept.source,
                )
                for chunk_concept in chunk_concepts
            ],
        )

    def _replace_chunk_concepts_cursor(
        self,
        cur,
        document_id: str,
        chunk_concepts: "list[StoredChunkConcept]",
    ) -> None:
        cur.execute(
            """
            DELETE FROM chunk_concepts
            WHERE chunk_id IN (
                SELECT chunk_id
                FROM chunks
                WHERE document_id = %s::uuid
                  AND deleted_at IS NULL
            );
            """,
            (document_id,),
        )
        self._upsert_chunk_concepts_cursor(cur, chunk_concepts)

    def _replace_lesson_cards_cursor(
        self,
        cur,
        document_id: str,
        lesson_cards: "list[StoredLessonCard]",
    ) -> None:
        cur.execute(
            """
            UPDATE lesson_cards
            SET deleted_at = NOW()
            WHERE EXISTS (
                SELECT 1
                FROM chunks c
                WHERE c.document_id = %s::uuid
                  AND c.chunk_id = ANY(lesson_cards.source_chunk_ids)
            )
              AND status IN ('GENERATED_DRAFT','REVIEWING','CHANGES_REQUESTED');
            """,
            (document_id,),
        )
        if not lesson_cards:
            return

        cur.executemany(
            """
            WITH chunk_context AS (
                SELECT c.course_id, clm.lo_id, lo.statement_vi
                FROM chunks c
                JOIN chunk_lo_mappings clm ON clm.chunk_id = c.chunk_id
                JOIN learning_outcomes lo ON lo.lo_id = clm.lo_id
                WHERE c.chunk_id = %s::uuid
                  AND c.deleted_at IS NULL
                ORDER BY clm.confidence DESC
                LIMIT 1
            ),
            lesson_row AS (
                INSERT INTO lessons (course_id, lo_id, title, status)
                SELECT course_id, lo_id, COALESCE(statement_vi, 'Generated lesson'), 'DRAFT'
                FROM chunk_context
                ON CONFLICT (course_id, lo_id) DO UPDATE SET updated_at = NOW()
                RETURNING lesson_id, course_id, lo_id
            )
            INSERT INTO lesson_cards (
                card_id,
                lesson_id,
                course_id,
                lo_id,
                title,
                content,
                source_chunk_ids,
                status,
                deleted_at
            )
            SELECT %s::uuid, lesson_id, course_id, lo_id, %s, %s::jsonb, %s::uuid[], 'GENERATED_DRAFT', NULL
            FROM lesson_row
            ON CONFLICT (card_id) DO UPDATE SET
                lesson_id = EXCLUDED.lesson_id,
                course_id = EXCLUDED.course_id,
                lo_id = EXCLUDED.lo_id,
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                source_chunk_ids = EXCLUDED.source_chunk_ids,
                status = 'GENERATED_DRAFT',
                updated_at = NOW(),
                deleted_at = NULL;
            """,
            [
                (
                    card.primary_chunk_id,
                    _stable_uuid(card.card_id),
                    card.title,
                    json.dumps({
                        "key_insight": card.key_insight,
                        "bullets": list(card.bullets),
                        "heading_path": list(card.heading_path),
                        "model_id": card.model_id,
                        "card_index": card.card_index,
                    }),
                    [_stable_uuid(chunk_id) for chunk_id in (card.source_chunk_ids or (card.primary_chunk_id,))],
                )
                for card in lesson_cards
            ],
        )

    def _replace_quiz_items_cursor(
        self,
        cur,
        document_id: str,
        quiz_items: "list[StoredQuizItem]",
    ) -> None:
        cur.execute(
            """
            UPDATE quiz_items
            SET deleted_at = NOW()
            WHERE EXISTS (
                SELECT 1
                FROM chunks c
                WHERE c.document_id = %s::uuid
                  AND c.chunk_id = ANY(quiz_items.source_chunk_ids)
            )
              AND status IN ('GENERATED_DRAFT','REVIEWING','CHANGES_REQUESTED');
            """,
            (document_id,),
        )
        if not quiz_items:
            return

        cur.executemany(
            """
            WITH chunk_context AS (
                SELECT c.course_id, clm.lo_id, lo.statement_vi
                FROM chunks c
                JOIN chunk_lo_mappings clm ON clm.chunk_id = c.chunk_id
                JOIN learning_outcomes lo ON lo.lo_id = clm.lo_id
                WHERE c.chunk_id = %s::uuid
                  AND c.deleted_at IS NULL
                ORDER BY clm.confidence DESC
                LIMIT 1
            ),
            lesson_row AS (
                INSERT INTO lessons (course_id, lo_id, title, status)
                SELECT course_id, lo_id, COALESCE(statement_vi, 'Generated lesson'), 'DRAFT'
                FROM chunk_context
                ON CONFLICT (course_id, lo_id) DO UPDATE SET updated_at = NOW()
                RETURNING lesson_id, course_id, lo_id
            )
            INSERT INTO quiz_items (
                quiz_id,
                lesson_id,
                course_id,
                lo_id,
                type,
                question,
                options,
                correct_answer,
                explanation,
                source_chunk_ids,
                status,
                deleted_at
            )
            SELECT %s::uuid, lesson_id, course_id, lo_id, 'MCQ_SINGLE', %s, %s::jsonb, %s::jsonb,
                   %s, %s::uuid[], 'GENERATED_DRAFT', NULL
            FROM lesson_row
            ON CONFLICT (quiz_id) DO UPDATE SET
                lesson_id = EXCLUDED.lesson_id,
                course_id = EXCLUDED.course_id,
                lo_id = EXCLUDED.lo_id,
                question = EXCLUDED.question,
                options = EXCLUDED.options,
                correct_answer = EXCLUDED.correct_answer,
                explanation = EXCLUDED.explanation,
                source_chunk_ids = EXCLUDED.source_chunk_ids,
                status = 'GENERATED_DRAFT',
                updated_at = NOW(),
                deleted_at = NULL;
            """,
            [
                (
                    item.primary_chunk_id,
                    _stable_uuid(item.question_id),
                    item.question,
                    json.dumps(list(item.choices)),
                    json.dumps(
                        item.choices[item.correct_index]
                        if 0 <= item.correct_index < len(item.choices)
                        else None
                    ),
                    item.explanation,
                    [_stable_uuid(chunk_id) for chunk_id in (item.source_chunk_ids or (item.primary_chunk_id,))],
                )
                for item in quiz_items
            ],
        )

    def _delete_pending_outbox_cursor(
        self,
        cur,
        aggregate_id: str,
        event_type: str | None,
    ) -> None:
        if event_type is None:
            return

        cur.execute(
            """
            DELETE FROM outbox_events
            WHERE aggregate_id = %s::uuid
              AND event_type = %s
              AND status = 'PENDING';
            """,
            (aggregate_id, event_type),
        )

    def _append_outbox_event_cursor(
        self,
        cur,
        event_id: str,
        event_type: str,
        aggregate_id: str,
        payload: dict,
    ) -> None:
        cur.execute(
            """
            INSERT INTO outbox_events (
                event_id,
                event_type,
                aggregate_type,
                aggregate_id,
                payload,
                status,
                retry_count,
                last_error
            )
            VALUES (%s::uuid, %s, 'document', %s::uuid, %s::jsonb, 'PENDING', 0, NULL);
            """,
            (event_id, event_type, aggregate_id, json.dumps(payload)),
        )

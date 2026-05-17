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

            self._ensure_schema(conn)
            self._schema_initialized = True

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
                               owner_id,
                               language
                        FROM documents_metadata
                        WHERE document_id = %s;
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
                        SELECT cm.chunk_id,
                               cm.document_id,
                               cm.chunk_index,
                               cm.heading_path,
                               cm.heading_level,
                               cm.page_number,
                               cm.content_length,
                               cm.language,
                               cc.content_text,
                               cc.embedding_input
                        FROM chunks_metadata cm
                        LEFT JOIN chunk_contents cc
                          ON cc.chunk_id = cm.chunk_id
                        WHERE cm.document_id = %s
                        ORDER BY cm.chunk_index ASC;
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
                        SELECT lc.id,
                               lc.document_id,
                               lc.primary_chunk_id,
                               lc.source_chunk_ids,
                               lc.heading_path,
                               lc.title,
                               lc.bullets,
                               lc.key_insight,
                               lc.card_index,
                               lc.model_id
                        FROM lesson_cards lc
                        INNER JOIN chunks_metadata cm
                          ON cm.chunk_id = lc.primary_chunk_id
                        WHERE lc.document_id = %s
                        ORDER BY cm.chunk_index ASC, lc.card_index ASC, lc.id ASC;
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
                        heading_path=tuple(row[4] or ()),
                        title=row[5],
                        bullets=tuple(row[6] or ()),
                        key_insight=row[7],
                        card_index=row[8],
                        model_id=row[9],
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
                        SELECT qi.id,
                               qi.document_id,
                               qi.primary_chunk_id,
                               qi.source_chunk_ids,
                               qi.heading_path,
                               qi.question,
                               qi.choices,
                               qi.correct_index,
                               qi.explanation,
                               qi.difficulty,
                               qi.question_index,
                               qi.model_id
                        FROM quiz_items qi
                        INNER JOIN chunks_metadata cm
                          ON cm.chunk_id = qi.primary_chunk_id
                        WHERE qi.document_id = %s
                        ORDER BY cm.chunk_index ASC, qi.question_index ASC, qi.id ASC;
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
                        heading_path=tuple(row[4] or ()),
                        question=row[5],
                        choices=tuple(row[6] or ()),
                        correct_index=row[7],
                        explanation=row[8],
                        difficulty=row[9],
                        question_index=row[10],
                        model_id=row[11],
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
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

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
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

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
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

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
                where_clause = "WHERE d.course_id = %s"
                params = [course_id, limit, offset]
            query = f"""
                SELECT d.document_id,
                       d.document_name,
                       d.doc_type,
                       d.status,
                       d.course_id,
                       d.created_at,
                       (SELECT count(*) FROM chunks_metadata c WHERE c.document_id = d.document_id) AS chunk_count
                FROM documents_metadata d
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
                        WHERE aggregate_id = %s
                          AND status = 'PENDING';
                        """,
                        (document_id,),
                    )
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
                        (
                            event_id,
                            _EVENT_DOCUMENT_DELETED,
                            document_id,
                            json.dumps({"document_id": document_id}),
                        ),
                    )
                    cur.execute("DELETE FROM chunks_metadata WHERE document_id = %s;", (document_id,))
                    cur.execute(
                        "DELETE FROM documents_metadata WHERE document_id = %s;",
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
                            SELECT status, error_msg, storage_key
                            FROM documents_metadata
                            WHERE document_id = %s;
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
                            course_id, code, title_vi, title_en, credits, semester,
                            source_document_id, extraction_confidence, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (course_id) DO UPDATE SET
                            code = EXCLUDED.code,
                            title_vi = EXCLUDED.title_vi,
                            title_en = EXCLUDED.title_en,
                            credits = EXCLUDED.credits,
                            semester = EXCLUDED.semester,
                            source_document_id = EXCLUDED.source_document_id,
                            extraction_confidence = EXCLUDED.extraction_confidence,
                            updated_at = NOW();
                        """,
                        (
                            course.course_id, course.code, course.title_vi, course.title_en,
                            course.credits, course.semester, course.source_document_id,
                            course.extraction_confidence,
                        ),
                    )

                    if chapters:
                        cur.executemany(
                            """
                            INSERT INTO chapters (chapter_id, course_id, code, title, order_index)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (course_id, code) DO UPDATE SET
                                title = EXCLUDED.title, order_index = EXCLUDED.order_index;
                            """,
                            [(c.chapter_id, c.course_id, c.code, c.title, c.order_index) for c in chapters],
                        )

                    if learning_outcomes:
                        cur.executemany(
                            """
                            INSERT INTO learning_outcomes (
                                lo_id, course_id, code, parent_code,
                                statement_vi, statement_en, bloom_level, cdio_level
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (course_id, code) DO UPDATE SET
                                parent_code = EXCLUDED.parent_code,
                                statement_vi = EXCLUDED.statement_vi,
                                statement_en = EXCLUDED.statement_en,
                                bloom_level = EXCLUDED.bloom_level,
                                cdio_level = EXCLUDED.cdio_level;
                            """,
                            [
                                (
                                    lo.lo_id, lo.course_id, lo.code, lo.parent_code,
                                    lo.statement_vi, lo.statement_en, lo.bloom_level, lo.cdio_level,
                                )
                                for lo in learning_outcomes
                            ],
                        )

                    if assessments:
                        cur.executemany(
                            """
                            INSERT INTO assessments (
                                assessment_id, course_id, code, name_vi, name_en, category, weight
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (course_id, code) DO UPDATE SET
                                name_vi = EXCLUDED.name_vi,
                                name_en = EXCLUDED.name_en,
                                category = EXCLUDED.category,
                                weight = EXCLUDED.weight;
                            """,
                            [
                                (a.assessment_id, a.course_id, a.code, a.name_vi, a.name_en, a.category, a.weight)
                                for a in assessments
                            ],
                        )

                    if lo_assessment_links:
                        cur.executemany(
                            """
                            INSERT INTO lo_assessments (lo_id, assessment_id)
                            VALUES (%s, %s)
                            ON CONFLICT DO NOTHING;
                            """,
                            lo_assessment_links,
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
                        "SELECT course_id, code, title_vi, title_en, credits, semester, "
                        "source_document_id, extraction_confidence FROM courses WHERE course_id = %s;",
                        (course_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return Ok(None)
                    course = StoredCourse(
                        course_id=row[0], code=row[1], title_vi=row[2], title_en=row[3],
                        credits=row[4], semester=row[5], source_document_id=row[6],
                        extraction_confidence=row[7],
                    )

                    cur.execute(
                        "SELECT chapter_id, course_id, code, title, order_index FROM chapters "
                        "WHERE course_id = %s ORDER BY order_index;",
                        (course_id,),
                    )
                    chapters = [
                        StoredChapter(chapter_id=r[0], course_id=r[1], code=r[2], title=r[3], order_index=r[4])
                        for r in cur.fetchall()
                    ]

                    cur.execute(
                        "SELECT lo_id, course_id, code, parent_code, statement_vi, statement_en, "
                        "bloom_level, cdio_level FROM learning_outcomes WHERE course_id = %s;",
                        (course_id,),
                    )
                    los = [
                        StoredLearningOutcome(
                            lo_id=r[0], course_id=r[1], code=r[2], parent_code=r[3],
                            statement_vi=r[4], statement_en=r[5], bloom_level=r[6], cdio_level=r[7],
                        )
                        for r in cur.fetchall()
                    ]

                    cur.execute(
                        "SELECT assessment_id, course_id, code, name_vi, name_en, category, weight "
                        "FROM assessments WHERE course_id = %s;",
                        (course_id,),
                    )
                    assessments = [
                        StoredAssessment(
                            assessment_id=r[0], course_id=r[1], code=r[2], name_vi=r[3],
                            name_en=r[4], category=r[5], weight=r[6],
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
                        FROM learning_outcomes
                        WHERE course_id = %s AND (code LIKE %s OR code = %s);
                        """,
                        (course_id, prefix + "%", f"L.O.{chapter_code}"),
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
                        SELECT lo.lo_id, lo.course_id, lo.code, lo.parent_code, lo.statement_vi,
                               lo.statement_en, lo.bloom_level, lo.cdio_level
                        FROM learning_outcomes lo
                        JOIN lo_assessments la ON la.lo_id = lo.lo_id
                        JOIN assessments a ON a.assessment_id = la.assessment_id
                        WHERE lo.course_id = %s AND a.code = %s;
                        """,
                        (course_id, assessment_code),
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
                        INSERT INTO chunk_lo_mappings (chunk_id, lo_id, confidence, source)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (chunk_id, lo_id) DO UPDATE SET
                            confidence = GREATEST(EXCLUDED.confidence, chunk_lo_mappings.confidence),
                            source = EXCLUDED.source;
                        """,
                        [(m.chunk_id, m.lo_id, m.confidence, m.source) for m in mappings],
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
                        SELECT cm.chunk_id, cm.document_id, cm.chunk_index,
                               cm.heading_path, cm.heading_level, cm.page_number,
                               cm.content_length, cm.language,
                               cc.content_text, cc.embedding_input
                        FROM chunk_lo_mappings clm
                        JOIN chunks_metadata cm ON cm.chunk_id = clm.chunk_id
                        LEFT JOIN chunk_contents cc ON cc.chunk_id = cm.chunk_id
                        WHERE clm.lo_id = %s
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
        return Document(
            id=row[0],
            name=row[1],
            path=None,
            doc_type=DocumentType(row[2]),
            size_bytes=row[4],
            mime_type=row[3],
            created_at=row[5],
        )

    def _upsert_chunks_cursor(self, cur, chunks: "list[StoredChunkMetadata]") -> None:
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
        content_rows = [
            (
                c.chunk_id,
                c.document_id,
                c.content_text or "",
                c.embedding_input,
            )
            for c in chunks
        ]
        if not rows:
            return

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
        cur.executemany(
            """
            INSERT INTO chunk_contents (
                chunk_id,
                document_id,
                content_text,
                embedding_input,
                updated_at
            )
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (chunk_id)
            DO UPDATE SET
                document_id = EXCLUDED.document_id,
                content_text = EXCLUDED.content_text,
                embedding_input = EXCLUDED.embedding_input,
                updated_at = NOW();
            """,
            content_rows,
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
                FROM chunks_metadata
                WHERE document_id = %s
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
            "DELETE FROM lesson_cards WHERE document_id = %s;",
            (document_id,),
        )
        if not lesson_cards:
            return

        cur.executemany(
            """
            INSERT INTO lesson_cards (
                id,
                document_id,
                primary_chunk_id,
                source_chunk_ids,
                heading_path,
                title,
                bullets,
                key_insight,
                card_index,
                model_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            [
                (
                    card.card_id,
                    card.document_id,
                    card.primary_chunk_id,
                    list(card.source_chunk_ids),
                    list(card.heading_path),
                    card.title,
                    list(card.bullets),
                    card.key_insight,
                    card.card_index,
                    card.model_id,
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
            "DELETE FROM quiz_items WHERE document_id = %s;",
            (document_id,),
        )
        if not quiz_items:
            return

        cur.executemany(
            """
            INSERT INTO quiz_items (
                id,
                document_id,
                primary_chunk_id,
                source_chunk_ids,
                heading_path,
                question,
                choices,
                correct_index,
                explanation,
                difficulty,
                question_index,
                model_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            [
                (
                    item.question_id,
                    item.document_id,
                    item.primary_chunk_id,
                    list(item.source_chunk_ids),
                    list(item.heading_path),
                    item.question,
                    list(item.choices),
                    item.correct_index,
                    item.explanation,
                    item.difficulty,
                    item.question_index,
                    item.model_id,
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
            WHERE aggregate_id = %s
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

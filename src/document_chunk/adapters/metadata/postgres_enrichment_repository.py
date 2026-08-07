from __future__ import annotations

import json
import uuid
from typing import Any

from document_chunk.adapters.metadata.postgres_repository_utils import (
    PostgresRepositoryBase,
    stable_uuid,
)
from document_chunk.domain.exceptions import MetadataStoreError
from document_chunk.domain.outbox_events import OutboxEventType
from document_chunk.domain.ports.metadata_store import (
    StoredChunkConcept,
    StoredConcept,
    StoredLessonCard,
    StoredQuizItem,
)
from document_chunk.shared.result import Err, Ok, Result


class PostgresEnrichmentRepository(PostgresRepositoryBase):
    """Generated lesson, quiz, and concept persistence operations."""

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
        outbox_event_type: OutboxEventType | None = None,
        outbox_payload: dict | None = None,
    ) -> Result[str | None, Exception]:
        event_id: str | None = None
        if outbox_event_type is not None:
            event_id = str(uuid.uuid4())
            if outbox_payload is None:
                return Err(
                    MetadataStoreError(
                        "outbox_payload is required when outbox_event_type is provided"
                    )
                )

        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    self._replace_lesson_cards_cursor(cur, document_id, lesson_cards)
                    self._replace_quiz_items_cursor(cur, document_id, quiz_items)
                    self._replace_chunk_concepts_cursor(cur, document_id, chunk_concepts)
                    self._upsert_concepts_cursor(cur, concepts)
                    if (
                        event_id is not None
                        and outbox_event_type is not None
                        and outbox_payload is not None
                    ):
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
                        correct_index=(
                            (row[5] or []).index(row[6]) if row[6] in (row[5] or []) else 0
                        ),
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

    def _upsert_concepts_cursor(
        self,
        cur: Any,
        concepts: "list[StoredConcept]",
    ) -> None:
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

    def _upsert_chunk_concepts_cursor(
        self,
        cur: Any,
        chunk_concepts: "list[StoredChunkConcept]",
    ) -> None:
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
        cur: Any,
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
        cur: Any,
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
                    stable_uuid(card.card_id),
                    card.title,
                    json.dumps(
                        {
                            "key_insight": card.key_insight,
                            "bullets": list(card.bullets),
                            "heading_path": list(card.heading_path),
                            "model_id": card.model_id,
                            "card_index": card.card_index,
                        }
                    ),
                    [
                        stable_uuid(chunk_id)
                        for chunk_id in (card.source_chunk_ids or (card.primary_chunk_id,))
                    ],
                )
                for card in lesson_cards
            ],
        )

    def _replace_quiz_items_cursor(
        self,
        cur: Any,
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
                    stable_uuid(item.question_id),
                    item.question,
                    json.dumps(list(item.choices)),
                    json.dumps(
                        item.choices[item.correct_index]
                        if 0 <= item.correct_index < len(item.choices)
                        else None
                    ),
                    item.explanation,
                    [
                        stable_uuid(chunk_id)
                        for chunk_id in (item.source_chunk_ids or (item.primary_chunk_id,))
                    ],
                )
                for item in quiz_items
            ],
        )

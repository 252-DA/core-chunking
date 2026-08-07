from __future__ import annotations

import json
import uuid

from document_chunk.adapters.metadata.postgres_repository_utils import (
    PostgresRepositoryBase,
    stable_uuid,
)
from document_chunk.domain.exceptions import MetadataStoreError
from document_chunk.domain.ports.metadata_store import (
    StoredAssessment,
    StoredChapter,
    StoredChunkLOMapping,
    StoredChunkMetadata,
    StoredCourse,
    StoredLearningOutcome,
    StoredQuizItem,
)
from document_chunk.shared.result import Err, Ok, Result


class PostgresCurriculumRepository(PostgresRepositoryBase):
    """Curriculum, learning-outcome, assessment, and LMS operations."""

    def persist_curriculum_quiz_items(
        self,
        lo_id: str,
        bloom_level: str | int | None,
        quiz_items: list[StoredQuizItem],
    ) -> Result[None, Exception]:
        if not quiz_items:
            return Ok(None)

        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        WITH target_context AS (
                            SELECT ch.course_id, lo.statement_vi
                            FROM learning_outcomes lo
                            JOIN chapters ch ON ch.chapter_id = lo.chapter_id
                            WHERE lo.lo_id = %s::uuid
                              AND lo.deleted_at IS NULL
                              AND lo.is_current = TRUE
                        ),
                        lesson_row AS (
                            INSERT INTO lessons (course_id, lo_id, title, status)
                            SELECT course_id, %s::uuid,
                                   COALESCE(statement_vi, 'Generated lesson'), 'DRAFT'
                            FROM target_context
                            ON CONFLICT (course_id, lo_id)
                            DO UPDATE SET updated_at = NOW()
                            RETURNING lesson_id, course_id
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
                            bloom_level,
                            source_chunk_ids,
                            status,
                            deleted_at
                        )
                        SELECT %s::uuid, lesson_id, course_id, %s::uuid,
                               'MCQ_SINGLE', %s, %s::jsonb, %s::jsonb, %s,
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
                            bloom_level = EXCLUDED.bloom_level,
                            source_chunk_ids = EXCLUDED.source_chunk_ids,
                            status = 'GENERATED_DRAFT',
                            updated_at = NOW(),
                            deleted_at = NULL;
                        """,
                        [
                            (
                                lo_id,
                                lo_id,
                                stable_uuid(item.question_id),
                                lo_id,
                                item.question,
                                json.dumps(list(item.choices)),
                                json.dumps(
                                    item.choices[item.correct_index]
                                    if 0 <= item.correct_index < len(item.choices)
                                    else None
                                ),
                                item.explanation,
                                self._bloom_level(bloom_level),
                                [
                                    stable_uuid(chunk_id)
                                    for chunk_id in (
                                        item.source_chunk_ids or (item.primary_chunk_id,)
                                    )
                                ],
                            )
                            for item in quiz_items
                        ],
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(
                MetadataStoreError(
                    "PostgreSQL curriculum quiz persistence failed",
                    cause=exc,
                )
            )

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
                            stable_uuid(course.course_id),
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
                                    stable_uuid(c.chapter_id),
                                    stable_uuid(c.course_id),
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
                                    stable_uuid(lo.lo_id),
                                    stable_uuid(
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
                                    stable_uuid(a.assessment_id),
                                    stable_uuid(a.course_id),
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
                            [
                                (stable_uuid(lo_id), stable_uuid(assessment_id))
                                for lo_id, assessment_id in lo_assessment_links
                            ],
                        )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def get_curriculum(self, course_id: str) -> Result[
        tuple[
            StoredCourse, list[StoredChapter], list[StoredLearningOutcome], list[StoredAssessment]
        ]
        | None,
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
                        (stable_uuid(course_id),),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return Ok(None)
                    course = StoredCourse(
                        course_id=row[0],
                        code=row[1],
                        title_vi=row[2],
                        title_en=row[3],
                    )

                    cur.execute(
                        """
                        SELECT chapter_id::text, course_id::text, title, sort_order
                        FROM chapters
                        WHERE course_id = %s::uuid
                          AND deleted_at IS NULL
                        ORDER BY sort_order;
                        """,
                        (stable_uuid(course_id),),
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
                        (stable_uuid(course_id),),
                    )
                    los = [
                        StoredLearningOutcome(
                            lo_id=r[0],
                            course_id=r[1],
                            code=r[2],
                            parent_code=r[3],
                            statement_vi=r[4],
                            statement_en=r[5],
                            bloom_level=r[6],
                            cdio_level=r[7],
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
                        (stable_uuid(course_id),),
                    )
                    assessments = [
                        StoredAssessment(
                            assessment_id=r[0],
                            course_id=r[1],
                            code=r[2],
                            name_vi=r[2],
                            name_en=None,
                            category=r[3],
                            weight=float(r[4]),
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
                        (stable_uuid(course_id), prefix + "%", f"L.O.{chapter_code}"),
                    )
                    return Ok(
                        [
                            StoredLearningOutcome(
                                lo_id=r[0],
                                course_id=r[1],
                                code=r[2],
                                parent_code=r[3],
                                statement_vi=r[4],
                                statement_en=r[5],
                                bloom_level=r[6],
                                cdio_level=r[7],
                            )
                            for r in cur.fetchall()
                        ]
                    )
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
                        (stable_uuid(course_id), assessment_code),
                    )
                    return Ok(
                        [
                            StoredLearningOutcome(
                                lo_id=r[0],
                                course_id=r[1],
                                code=r[2],
                                parent_code=r[3],
                                statement_vi=r[4],
                                statement_en=r[5],
                                bloom_level=r[6],
                                cdio_level=r[7],
                            )
                            for r in cur.fetchall()
                        ]
                    )
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

    def list_chunks_for_lo(self, lo_id: str) -> Result[list[StoredChunkMetadata], Exception]:
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
                    return Ok(
                        [
                            StoredChunkMetadata(
                                chunk_id=r[0],
                                document_id=r[1],
                                chunk_index=r[2],
                                heading_path=tuple(r[3] or ()),
                                heading_level=r[4],
                                page_number=r[5],
                                content_length=r[6],
                                language=r[7],
                                content_text=r[8],
                                embedding_input=r[9],
                            )
                            for r in cur.fetchall()
                        ]
                    )
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def update_content_generation_request(
        self,
        request_id: str,
        status: str,
        generated_count: int | None = None,
        last_error: str | None = None,
    ) -> Result[None, Exception]:
        allowed_statuses = {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}
        if status not in allowed_statuses:
            return Err(MetadataStoreError(f"Invalid content-generation status: {status}"))
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE content_generation_requests
                        SET status = %s,
                            generated_count = COALESCE(%s, generated_count),
                            last_error = %s,
                            updated_at = NOW()
                        WHERE request_id = %s::uuid;
                        """,
                        (status, generated_count, last_error, request_id),
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(
                MetadataStoreError(
                    "PostgreSQL metadata store operation failed",
                    cause=exc,
                )
            )

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

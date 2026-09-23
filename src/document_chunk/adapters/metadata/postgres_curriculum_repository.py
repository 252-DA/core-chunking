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
    StoredChapterLOLink,
    StoredChunkLOMapping,
    StoredChunkMetadata,
    StoredCourse,
    StoredCourseGoal,
    StoredCourseSession,
    StoredExtractionIssue,
    StoredLearningOutcome,
    StoredLOAssessmentLink,
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
                            SELECT lo.course_id, lo.statement_vi
                            FROM learning_outcomes lo
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
        lo_assessment_links: list[StoredLOAssessmentLink],
        chapter_lo_links: list[StoredChapterLOLink] | None = None,
        goals: list[StoredCourseGoal] | None = None,
        sessions: list[StoredCourseSession] | None = None,
        issues: list[StoredExtractionIssue] | None = None,
    ) -> Result[None, Exception]:
        chapter_lo_links = chapter_lo_links or []
        goals = goals or []
        sessions = sessions or []
        issues = issues or []
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    # `courses.code` cũng là khoá unique, nên một học phần do
                    # LMS tạo trước sẽ có course_id khác stable_uuid(code). Bám
                    # theo hàng đang có thay vì chèn thêm một học phần trùng mã.
                    course_uuid = self._course_uuid(cur, course.code)

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
                            course_uuid,
                            course.code,
                            course.title_vi,
                            course.title_en or course.semester,
                        ),
                    )

                    if chapters:
                        cur.executemany(
                            """
                            INSERT INTO chapters (
                                chapter_id, course_id, code, title, title_en,
                                sort_order, source_section, source_page, deleted_at
                            )
                            VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, NULL)
                            ON CONFLICT (chapter_id) DO UPDATE SET
                                code = EXCLUDED.code,
                                title = EXCLUDED.title,
                                title_en = EXCLUDED.title_en,
                                sort_order = EXCLUDED.sort_order,
                                source_section = EXCLUDED.source_section,
                                source_page = EXCLUDED.source_page,
                                deleted_at = NULL;
                            """,
                            [
                                (
                                    stable_uuid(c.chapter_id),
                                    course_uuid,
                                    c.code,
                                    c.title,
                                    c.title_en,
                                    c.order_index,
                                    c.source_section,
                                    c.source_page,
                                )
                                for c in chapters
                            ],
                        )

                    if learning_outcomes:
                        # LO gắn vào HỌC PHẦN. Không còn suy số chương từ mã LO:
                        # quan hệ chương ↔ LO nằm ở chapter_los bên dưới.
                        cur.executemany(
                            """
                            INSERT INTO learning_outcomes (
                                lo_id, course_id, code, parent_code,
                                statement_vi, statement_en,
                                bloom_level, bloom_provenance,
                                cdio_level, cdio_provenance,
                                source_section, source_page,
                                academic_year, version, is_current, deleted_at
                            )
                            VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, 'default', 1, TRUE, NULL)
                            ON CONFLICT (course_id, code, academic_year, version) DO UPDATE SET
                                parent_code = EXCLUDED.parent_code,
                                statement_vi = EXCLUDED.statement_vi,
                                statement_en = EXCLUDED.statement_en,
                                bloom_level = EXCLUDED.bloom_level,
                                bloom_provenance = EXCLUDED.bloom_provenance,
                                cdio_level = EXCLUDED.cdio_level,
                                cdio_provenance = EXCLUDED.cdio_provenance,
                                source_section = EXCLUDED.source_section,
                                source_page = EXCLUDED.source_page,
                                is_current = TRUE,
                                deleted_at = NULL;
                            """,
                            [
                                (
                                    stable_uuid(lo.lo_id),
                                    course_uuid,
                                    lo.code,
                                    lo.parent_code,
                                    lo.statement_vi,
                                    lo.statement_en,
                                    self._bloom_level(lo.bloom_level),
                                    lo.bloom_provenance,
                                    self._cdio_level(lo.cdio_level),
                                    lo.cdio_provenance,
                                    lo.source_section,
                                    lo.source_page,
                                )
                                for lo in learning_outcomes
                            ],
                        )

                    if chapter_lo_links:
                        cur.executemany(
                            """
                            INSERT INTO chapter_los (
                                chapter_id, lo_id, provenance, source_section, source_page
                            )
                            VALUES (%s::uuid, %s::uuid, %s, %s, %s)
                            ON CONFLICT (chapter_id, lo_id) DO UPDATE SET
                                provenance = EXCLUDED.provenance,
                                source_section = EXCLUDED.source_section,
                                source_page = EXCLUDED.source_page;
                            """,
                            [
                                (
                                    stable_uuid(link.chapter_id),
                                    stable_uuid(link.lo_id),
                                    link.provenance,
                                    link.source_section,
                                    link.source_page,
                                )
                                for link in chapter_lo_links
                            ],
                        )

                    if goals:
                        cur.executemany(
                            """
                            INSERT INTO course_goals (
                                course_id, code, statement_vi, statement_en,
                                source_section, source_page
                            )
                            VALUES (%s::uuid, %s, %s, %s, %s, %s)
                            ON CONFLICT (course_id, code) DO UPDATE SET
                                statement_vi = EXCLUDED.statement_vi,
                                statement_en = EXCLUDED.statement_en,
                                source_section = EXCLUDED.source_section,
                                source_page = EXCLUDED.source_page;
                            """,
                            [
                                (
                                    course_uuid, g.code, g.statement_vi,
                                    g.statement_en, g.source_section, g.source_page,
                                )
                                for g in goals
                            ],
                        )

                    if sessions:
                        cur.executemany(
                            """
                            INSERT INTO course_sessions (
                                course_id, order_index, session_no, chapter_id,
                                title_vi, title_en, source_section, source_page
                            )
                            VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (course_id, order_index) DO UPDATE SET
                                session_no = EXCLUDED.session_no,
                                chapter_id = EXCLUDED.chapter_id,
                                title_vi = EXCLUDED.title_vi,
                                title_en = EXCLUDED.title_en,
                                source_section = EXCLUDED.source_section,
                                source_page = EXCLUDED.source_page;
                            """,
                            [
                                (
                                    course_uuid, s.order_index, s.session_no,
                                    stable_uuid(s.chapter_id) if s.chapter_id else None,
                                    s.title_vi, s.title_en, s.source_section, s.source_page,
                                )
                                for s in sessions
                            ],
                        )

                    if assessments:
                        cur.executemany(
                            """
                            INSERT INTO assessments (
                                assessment_id, course_id, code, parent_code,
                                title, title_en, activity_type,
                                max_points, weight, weight_provenance,
                                sort_order, type, source_section, source_page, deleted_at
                            )
                            VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s, %s, %s, NULL)
                            ON CONFLICT (assessment_id) DO UPDATE SET
                                code = EXCLUDED.code,
                                parent_code = EXCLUDED.parent_code,
                                title = EXCLUDED.title,
                                title_en = EXCLUDED.title_en,
                                activity_type = EXCLUDED.activity_type,
                                max_points = EXCLUDED.max_points,
                                weight = EXCLUDED.weight,
                                weight_provenance = EXCLUDED.weight_provenance,
                                sort_order = EXCLUDED.sort_order,
                                type = EXCLUDED.type,
                                source_section = EXCLUDED.source_section,
                                source_page = EXCLUDED.source_page,
                                deleted_at = NULL;
                            """,
                            [
                                (
                                    stable_uuid(a.assessment_id),
                                    course_uuid,
                                    a.code,
                                    a.parent_code,
                                    a.name_vi,
                                    a.name_en,
                                    a.activity_type,
                                    100,
                                    a.weight,
                                    a.weight_provenance,
                                    index + 1,
                                    self._assessment_type(a.category),
                                    a.source_section,
                                    a.source_page,
                                )
                                for index, a in enumerate(assessments)
                            ],
                        )

                    if lo_assessment_links:
                        cur.executemany(
                            """
                            INSERT INTO lo_assessments (
                                lo_id, assessment_id, session_order, scope, chapter_id,
                                provenance, source_section, source_page
                            )
                            VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (lo_id, assessment_id, session_order) DO UPDATE SET
                                scope = EXCLUDED.scope,
                                chapter_id = EXCLUDED.chapter_id,
                                provenance = EXCLUDED.provenance,
                                source_section = EXCLUDED.source_section,
                                source_page = EXCLUDED.source_page;
                            """,
                            [
                                (
                                    stable_uuid(link.lo_id),
                                    stable_uuid(link.assessment_id),
                                    link.session_order,
                                    link.scope,
                                    stable_uuid(link.chapter_id) if link.chapter_id else None,
                                    link.provenance,
                                    link.source_section,
                                    link.source_page,
                                )
                                for link in lo_assessment_links
                            ],
                        )

                    # Ghi lại kết quả soát của lần trích này. Xoá các issue chưa
                    # xử lý của lần trước để màn hình kiểm tra không tồn đọng.
                    cur.execute(
                        "DELETE FROM curriculum_extraction_issues "
                        "WHERE course_id = %s::uuid AND resolved_at IS NULL;",
                        (course_uuid,),
                    )
                    if issues:
                        cur.executemany(
                            """
                            INSERT INTO curriculum_extraction_issues (
                                course_id, document_id, code, severity, message,
                                source_section, source_page, source_locator
                            )
                            VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s);
                            """,
                            [
                                (
                                    course_uuid,
                                    stable_uuid(i.document_id) if i.document_id else None,
                                    i.code, i.severity, i.message,
                                    i.source_section, i.source_page, i.source_locator,
                                )
                                for i in issues
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
                        (self._course_uuid(cur, course_id),),
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
                        SELECT chapter_id::text, course_id::text, title, sort_order, code, title_en
                        FROM chapters
                        WHERE course_id = %s::uuid
                          AND deleted_at IS NULL
                        ORDER BY sort_order;
                        """,
                        (self._course_uuid(cur, course_id),),
                    )
                    chapters = [
                        StoredChapter(
                            chapter_id=r[0],
                            course_id=r[1],
                            code=r[4],
                            title=r[2],
                            order_index=r[3],
                            title_en=r[5],
                        )
                        for r in cur.fetchall()
                    ]

                    cur.execute(
                        """
                        SELECT lo.lo_id::text, lo.course_id::text, lo.code, lo.parent_code,
                               lo.statement_vi, lo.statement_en, lo.bloom_level, lo.cdio_level,
                               lo.bloom_provenance, lo.cdio_provenance,
                               lo.source_section, lo.source_page
                        FROM learning_outcomes lo
                        WHERE lo.course_id = %s::uuid
                          AND lo.deleted_at IS NULL
                          AND lo.is_current = TRUE
                        ORDER BY lo.code;
                        """,
                        (self._course_uuid(cur, course_id),),
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
                            bloom_provenance=r[8],
                            cdio_provenance=r[9],
                            source_section=r[10],
                            source_page=r[11],
                        )
                        for r in cur.fetchall()
                    ]

                    cur.execute(
                        """
                        SELECT assessment_id::text, course_id::text, code, title, type,
                               weight, parent_code, activity_type, title_en
                        FROM assessments
                        WHERE course_id = %s::uuid
                          AND deleted_at IS NULL;
                        """,
                        (self._course_uuid(cur, course_id),),
                    )
                    assessments = [
                        StoredAssessment(
                            assessment_id=r[0],
                            course_id=r[1],
                            code=r[2],
                            name_vi=r[3],
                            name_en=r[8],
                            category=r[4],
                            weight=float(r[5]) if r[5] is not None else None,
                            parent_code=r[6],
                            activity_type=r[7],
                        )
                        for r in cur.fetchall()
                    ]
            return Ok((course, chapters, los, assessments))
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def list_chapter_lo_links(
        self, course_id: str
    ) -> Result[list[StoredChapterLOLink], Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT cl.chapter_id::text, cl.lo_id::text, cl.provenance,
                               cl.source_section, cl.source_page
                        FROM chapter_los cl
                        JOIN chapters ch ON ch.chapter_id = cl.chapter_id
                        JOIN learning_outcomes lo ON lo.lo_id = cl.lo_id
                        WHERE ch.course_id = %s::uuid
                          AND ch.deleted_at IS NULL
                          AND lo.deleted_at IS NULL
                          AND lo.is_current = TRUE
                        ORDER BY ch.sort_order, lo.code;
                        """,
                        (self._course_uuid(cur, course_id),),
                    )
                    return Ok([
                        StoredChapterLOLink(
                            chapter_id=r[0], lo_id=r[1], provenance=r[2],
                            source_section=r[3], source_page=r[4],
                        )
                        for r in cur.fetchall()
                    ])
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def list_los_by_chapter(
        self, course_id: str, chapter_code: str
    ) -> Result[list[StoredLearningOutcome], Exception]:
        """
        LO của một chương, đọc từ bảng nối chapter_los.

        Trước đây hàm này lọc bằng ``code LIKE 'L.O.<chương>.%'`` — tức là coi số
        đầu của mã LO là số chương. Đề cương không nói vậy: L.O.2.2 được dạy ở
        chương 4, 5, 7 và 12.
        """
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT lo.lo_id::text, lo.course_id::text, lo.code, lo.parent_code,
                               lo.statement_vi, lo.statement_en, lo.bloom_level, lo.cdio_level,
                               lo.bloom_provenance, lo.cdio_provenance,
                               lo.source_section, lo.source_page
                        FROM learning_outcomes lo
                        JOIN chapter_los cl ON cl.lo_id = lo.lo_id
                        JOIN chapters ch ON ch.chapter_id = cl.chapter_id
                        WHERE lo.course_id = %s::uuid
                          AND ch.code = %s
                          AND ch.deleted_at IS NULL
                          AND lo.deleted_at IS NULL
                          AND lo.is_current = TRUE
                        ORDER BY lo.code;
                        """,
                        (self._course_uuid(cur, course_id), chapter_code),
                    )
                    return Ok([self._row_to_lo(r) for r in cur.fetchall()])
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def list_chapters_for_lo(
        self, course_id: str, lo_code: str
    ) -> Result[list[StoredChapter], Exception]:
        """Chiều ngược lại: một LO được dạy ở những chương nào."""
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT ch.chapter_id::text, ch.course_id::text, ch.title,
                               ch.sort_order, ch.code, ch.title_en
                        FROM chapters ch
                        JOIN chapter_los cl ON cl.chapter_id = ch.chapter_id
                        JOIN learning_outcomes lo ON lo.lo_id = cl.lo_id
                        WHERE lo.course_id = %s::uuid
                          AND lo.code = %s
                          AND ch.deleted_at IS NULL
                          AND lo.deleted_at IS NULL
                          AND lo.is_current = TRUE
                        ORDER BY ch.sort_order;
                        """,
                        (self._course_uuid(cur, course_id), lo_code),
                    )
                    return Ok([
                        StoredChapter(
                            chapter_id=r[0], course_id=r[1], code=r[4],
                            title=r[2], order_index=r[3], title_en=r[5],
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
                        SELECT DISTINCT
                               lo.lo_id::text, lo.course_id::text, lo.code, lo.parent_code,
                               lo.statement_vi, lo.statement_en, lo.bloom_level, lo.cdio_level,
                               lo.bloom_provenance, lo.cdio_provenance,
                               lo.source_section, lo.source_page
                        FROM learning_outcomes lo
                        JOIN lo_assessments la ON la.lo_id = lo.lo_id
                        JOIN assessments a ON a.assessment_id = la.assessment_id
                        WHERE lo.course_id = %s::uuid
                          AND a.code = %s
                          AND lo.deleted_at IS NULL
                          AND lo.is_current = TRUE;
                        """,
                        (self._course_uuid(cur, course_id), assessment_code),
                    )
                    return Ok([self._row_to_lo(r) for r in cur.fetchall()])
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
                        INSERT INTO chunk_lo_mappings (
                            chunk_id, lo_id, confidence, source, provenance
                        )
                        VALUES (%s::uuid, %s::uuid, %s, %s, %s)
                        ON CONFLICT (chunk_id, lo_id) DO UPDATE SET
                            confidence = GREATEST(EXCLUDED.confidence, chunk_lo_mappings.confidence),
                            source = EXCLUDED.source,
                            -- Cạnh giảng viên đã xác nhận không bị hạ lại thành phỏng đoán.
                            provenance = CASE
                                WHEN chunk_lo_mappings.provenance = 'confirmed' THEN 'confirmed'
                                ELSE EXCLUDED.provenance
                            END;
                        """,
                        [
                            (m.chunk_id, m.lo_id, m.confidence, m.source, m.provenance)
                            for m in mappings
                        ],
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

    def _course_uuid(self, cur, course_ref: str) -> str:
        """
        Giải quyết tham chiếu học phần thành course_id thật.

        ``course_ref`` có thể là mã ("CO3011") hoặc course_id. Học phần do LMS
        tạo trước sẽ có course_id không bằng ``stable_uuid(mã)``, nên tra theo cả
        hai chiều thay vì giả định.
        """
        cur.execute(
            "SELECT course_id::text FROM courses "
            "WHERE code = %s OR course_id = %s::uuid LIMIT 1;",
            (course_ref, stable_uuid(course_ref)),
        )
        row = cur.fetchone()
        return row[0] if row else stable_uuid(course_ref)

    def _row_to_lo(self, r) -> StoredLearningOutcome:
        return StoredLearningOutcome(
            lo_id=r[0], course_id=r[1], code=r[2], parent_code=r[3],
            statement_vi=r[4], statement_en=r[5],
            bloom_level=r[6], cdio_level=r[7],
            bloom_provenance=r[8], cdio_provenance=r[9],
            source_section=r[10], source_page=r[11],
        )

    def _assessment_type(self, category: str) -> str:
        """Ánh xạ sang tập giá trị mà check constraint `assessments_type_check` cho phép."""
        return {
            "quiz": "QUIZ",
            "group_quiz": "PROJECT",   # GPJ-Project nhóm trong DCMH
            "project": "PROJECT",
            "midterm": "MIDTERM",
            "final": "FINAL_EXAM",
        }.get((category or "").lower(), "ASSIGNMENT")

    def _bloom_level(self, value: str | int | None) -> int | None:
        """
        None khi không biết — KHÔNG mặc định về 2 ("understand").

        Đề cương định dạng này không ghi Bloom. Trả về một mức mặc định sẽ khiến
        cột bloom_level luôn chứa dữ liệu bịa mà không cách nào phân biệt với
        giá trị thật.
        """
        if isinstance(value, int) and 1 <= value <= 6:
            return value
        return {
            "remember": 1, "understand": 2, "apply": 3,
            "analyze": 4, "evaluate": 5, "create": 6,
        }.get(str(value or "").lower())

    def _cdio_level(self, value: str | int | None) -> str | None:
        """None khi không biết — CDIO cũng không có trong đề cương."""
        if str(value) in {"I", "II", "III"}:
            return str(value)
        return {1: "I", 2: "II", 3: "III"}.get(value)

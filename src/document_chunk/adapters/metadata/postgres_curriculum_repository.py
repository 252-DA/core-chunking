from __future__ import annotations

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
    StoredLearningOutcome,
)
from document_chunk.shared.result import Err, Ok, Result


class PostgresCurriculumRepository(PostgresRepositoryBase):
    """Đọc đề cương (courses/chapters/LO/assessments) và ghi cạnh chunk → LO.

    Đề cương do core-api ghi (curriculum-writer.ts); phía Python chỉ đọc, và ghi
    ``chunk_lo_mappings`` — bảng AI do worker sở hữu.
    """

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
                        SELECT chapter_id::text, course_id::text, title, sort_order, code, title_en,
                               topics
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
                            topics=r[6],
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
                        JOIN documents d ON d.document_id = c.document_id
                        WHERE clm.lo_id = %s::uuid
                          AND c.deleted_at IS NULL
                          AND d.deleted_at IS NULL
                        -- Bài giảng là nguồn chính; tài liệu tham khảo chỉ bổ
                        -- sung khi bài giảng không đủ chunk cho LO này.
                        ORDER BY (d.role = 'reference'), clm.confidence DESC;
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

"""
Mồi một Curriculum vào DB test bằng SQL viết thẳng trong test.

Từ 2026-11-07 code chạy thật không còn ghi đề cương từ Python (core-api ghi khi
giảng viên áp dụng, core-api/src/modules/curriculum/services/curriculum-writer.ts) và
``IMetadataStore.upsert_curriculum`` đã bị xoá. Các test tích hợp đọc đề cương
(repository đọc, MapChunksToLos) vẫn cần dữ liệu mồi, nên helper này tự INSERT
9 bảng đề cương: giữ đúng id (``stable_uuid``), đúng cột và cùng cách ánh xạ như
``upsert_curriculum`` cũ để test vẫn kiểm đúng ý. Chỉ dành cho test — đừng
import từ code chạy thật.
"""
from __future__ import annotations

from document_chunk.adapters.metadata.postgres_repository_utils import stable_uuid
from document_chunk.domain.bloom import bloom_to_level
from document_chunk.domain.entities.curriculum import Curriculum
from document_chunk.shared.result import Err, Ok, Result


def _assessment_type(category: str) -> str:
    """Giá trị mà check constraint ``assessments_type_check`` cho phép."""
    return {
        "quiz": "QUIZ",
        "group_quiz": "PROJECT",
        "project": "PROJECT",
        "midterm": "MIDTERM",
        "final": "FINAL_EXAM",
    }.get((category or "").lower(), "ASSIGNMENT")


def _cdio_level(value) -> str | None:
    if str(value) in {"I", "II", "III"}:
        return str(value)
    return {1: "I", 2: "II", 3: "III"}.get(value)


def _course_uuid(cur, course_ref: str) -> str:
    # courses.code cũng unique: học phần do LMS tạo trước có course_id khác
    # stable_uuid(mã), nên bám theo hàng đang có thay vì chèn thêm học phần trùng mã.
    cur.execute(
        "SELECT course_id::text FROM courses WHERE code = %s OR course_id = %s::uuid LIMIT 1;",
        (course_ref, stable_uuid(course_ref)),
    )
    row = cur.fetchone()
    return row[0] if row else stable_uuid(course_ref)


def _section(ref):
    return ref.section if ref else None


def _page(ref):
    return ref.page if ref else None


def seed_curriculum(store, curriculum: Curriculum) -> Result[None, Exception]:
    course = curriculum.course
    chapter_id_by_code = {ch.code: ch.chapter_id for ch in curriculum.chapters}
    lo_id_by_code = {lo.code: lo.lo_id for lo in curriculum.learning_outcomes}
    ao_id_by_code = {a.code: a.assessment_id for a in curriculum.assessments}

    try:
        with store._connection() as conn:
            with conn.cursor() as cur:
                course_uuid = _course_uuid(cur, course.course_id or course.code)

                cur.execute(
                    """
                    INSERT INTO courses (course_id, code, name, description, updated_at)
                    VALUES (%s::uuid, %s, %s, %s, NOW())
                    ON CONFLICT (course_id) DO UPDATE SET
                        code = EXCLUDED.code,
                        name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        updated_at = NOW();
                    """,
                    (course_uuid, course.code, course.title_vi, course.title_en or course.semester),
                )

                if curriculum.chapters:
                    cur.executemany(
                        """
                        INSERT INTO chapters (
                            chapter_id, course_id, code, title, title_en,
                            sort_order, source_section, source_page, topics, deleted_at
                        )
                        VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, NULL)
                        ON CONFLICT (chapter_id) DO UPDATE SET
                            code = EXCLUDED.code,
                            title = EXCLUDED.title,
                            title_en = EXCLUDED.title_en,
                            sort_order = EXCLUDED.sort_order,
                            source_section = EXCLUDED.source_section,
                            source_page = EXCLUDED.source_page,
                            topics = EXCLUDED.topics,
                            deleted_at = NULL;
                        """,
                        [
                            (
                                stable_uuid(ch.chapter_id),
                                course_uuid,
                                ch.code,
                                ch.title,
                                ch.title_en,
                                ch.order_index,
                                _section(ch.source),
                                _page(ch.source),
                                "; ".join(ch.topics) or None,
                            )
                            for ch in curriculum.chapters
                        ],
                    )

                if curriculum.learning_outcomes:
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
                                bloom_to_level(lo.bloom_level),
                                lo.bloom_provenance,
                                _cdio_level(lo.cdio_level),
                                lo.cdio_provenance,
                                _section(lo.source),
                                _page(lo.source),
                            )
                            for lo in curriculum.learning_outcomes
                        ],
                    )

                chapter_lo_links = [
                    link
                    for link in curriculum.chapter_lo_links
                    if link.chapter_code in chapter_id_by_code and link.lo_code in lo_id_by_code
                ]
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
                                stable_uuid(chapter_id_by_code[link.chapter_code]),
                                stable_uuid(lo_id_by_code[link.lo_code]),
                                link.provenance,
                                _section(link.source),
                                _page(link.source),
                            )
                            for link in chapter_lo_links
                        ],
                    )

                if curriculum.goals:
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
                                course_uuid,
                                g.code,
                                g.statement_vi,
                                g.statement_en,
                                _section(g.source),
                                _page(g.source),
                            )
                            for g in curriculum.goals
                        ],
                    )

                if curriculum.sessions:
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
                                course_uuid,
                                row.order_index,
                                row.session_no,
                                (
                                    stable_uuid(chapter_id_by_code[row.chapter_code])
                                    if row.chapter_code and row.chapter_code in chapter_id_by_code
                                    else None
                                ),
                                row.title_vi,
                                row.title_en,
                                _section(row.source),
                                _page(row.source),
                            )
                            for row in curriculum.sessions
                        ],
                    )

                if curriculum.assessments:
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
                                _assessment_type(a.category),
                                _section(a.source),
                                _page(a.source),
                            )
                            for index, a in enumerate(curriculum.assessments)
                        ],
                    )

                lo_assessment_links = [
                    link
                    for link in curriculum.lo_assessment_links
                    if link.lo_code in lo_id_by_code and link.assessment_code in ao_id_by_code
                ]
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
                                stable_uuid(lo_id_by_code[link.lo_code]),
                                stable_uuid(ao_id_by_code[link.assessment_code]),
                                link.row_order_index or 0,
                                "session" if link.row_order_index is not None else "course",
                                (
                                    stable_uuid(chapter_id_by_code[link.chapter_code])
                                    if link.chapter_code and link.chapter_code in chapter_id_by_code
                                    else None
                                ),
                                link.provenance,
                                _section(link.source),
                                _page(link.source),
                            )
                            for link in lo_assessment_links
                        ],
                    )

                # Issue chưa xử lý của lần trích trước bị thay bằng lần này.
                cur.execute(
                    "DELETE FROM curriculum_extraction_issues "
                    "WHERE course_id = %s::uuid AND resolved_at IS NULL;",
                    (course_uuid,),
                )
                if curriculum.issues:
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
                                (
                                    stable_uuid(curriculum.source_document_id)
                                    if curriculum.source_document_id
                                    else None
                                ),
                                i.code,
                                i.severity,
                                i.message,
                                _section(i.source),
                                _page(i.source),
                                i.source.locator if i.source else None,
                            )
                            for i in curriculum.issues
                        ],
                    )
            conn.commit()
        return Ok(None)
    except Exception as exc:
        return Err(exc)

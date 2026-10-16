from __future__ import annotations

from document_chunk.adapters.metadata.postgres_repository_utils import (
    PostgresRepositoryBase,
    stable_uuid,
)
from document_chunk.domain.exceptions import MetadataStoreError
from document_chunk.domain.ports.metadata_store import StoredAnswerForGrading
from document_chunk.shared.result import Err, Ok, Result


class PostgresAnswerGradingRepository(PostgresRepositoryBase):
    """
    Chấm tự luận ngắn: đọc câu trả lời, ghi điểm, mở khoá AGS cho cả lượt nộp.

    core-api ghi các dòng quiz_attempts với grading_status PENDING (câu tự
    luận), đề lúc nộp trong grading_input và ags_status AWAITING_GRADE (cả
    lượt), rồi phát ANSWER_GRADING_REQUESTED. Xem core-api quiz/answer-grading.ts.
    """

    def list_answers_for_grading(
        self,
        attempt_ids: list[str],
    ) -> Result[list[StoredAnswerForGrading], Exception]:
        if not attempt_ids:
            return Ok([])
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    # Đề lấy từ grading_input (đóng băng lúc nộp), không JOIN
                    # quiz_items: câu trong ngân hàng có thể đã bị sửa hoặc xoá.
                    cur.execute(
                        """
                        SELECT attempt_id, quiz_id, course_id,
                               grading_input->>'type', grading_input->>'question',
                               grading_input->'correct_answer', chosen_answer,
                               grading_status
                        FROM quiz_attempts
                        WHERE attempt_id = ANY(%s::uuid[])
                          AND deleted_at IS NULL;
                        """,
                        ([stable_uuid(i) for i in attempt_ids],),
                    )
                    rows = cur.fetchall()
            return Ok([
                StoredAnswerForGrading(
                    attempt_id=str(row[0]),
                    quiz_id=str(row[1]),
                    course_id=str(row[2]),
                    question_type=row[3],
                    question=row[4],
                    correct_answer=row[5],
                    chosen_answer=row[6],
                    grading_status=row[7],
                )
                for row in rows
            ])
        except Exception as exc:
            return Err(MetadataStoreError(f"list_answers_for_grading failed: {exc}"))

    def save_answer_grade(
        self,
        *,
        attempt_id: str,
        score: float,
        is_correct: bool,
        feedback: str | None,
    ) -> Result[bool, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE quiz_attempts
                        SET score = %s, is_correct = %s, feedback = %s,
                            grading_status = 'GRADED'
                        WHERE attempt_id = %s::uuid
                          AND grading_status = 'PENDING';
                        """,
                        (score, is_correct, feedback, stable_uuid(attempt_id)),
                    )
                    updated = cur.rowcount == 1
                conn.commit()
            return Ok(updated)
        except Exception as exc:
            return Err(MetadataStoreError(f"save_answer_grade failed: {exc}"))

    def mark_answer_needs_review(
        self,
        *,
        attempt_id: str,
        feedback: str,
    ) -> Result[bool, Exception]:
        # Không đụng score: giá trị tạm lúc nộp, release_graded_attempts không
        # mở AGS khi còn câu NEEDS_REVIEW nên nó không lên sổ điểm.
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE quiz_attempts
                        SET grading_status = 'NEEDS_REVIEW', feedback = %s
                        WHERE attempt_id = %s::uuid
                          AND grading_status = 'PENDING';
                        """,
                        (feedback, stable_uuid(attempt_id)),
                    )
                    updated = cur.rowcount == 1
                conn.commit()
            return Ok(updated)
        except Exception as exc:
            return Err(MetadataStoreError(f"mark_answer_needs_review failed: {exc}"))

    def release_graded_attempts(self, attempt_ids: list[str]) -> Result[int, Exception]:
        if not attempt_ids:
            return Ok(0)
        ids = [stable_uuid(i) for i in attempt_ids]
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    # Khoá cả lượt nộp trước khi kiểm tra, cùng thứ tự với
                    # core-api lockAwaitingGroup: giảng viên chấm tay câu khác
                    # cùng lúc thì một trong hai bên chắc chắn thấy đủ điểm.
                    cur.execute(
                        """
                        SELECT attempt_id FROM quiz_attempts
                        WHERE attempt_id = ANY(%s::uuid[])
                        ORDER BY attempt_id FOR UPDATE;
                        """,
                        (ids,),
                    )
                    cur.execute(
                        """
                        UPDATE quiz_attempts
                        SET ags_status = 'PENDING'
                        WHERE attempt_id = ANY(%s::uuid[])
                          AND ags_status = 'AWAITING_GRADE'
                          AND NOT EXISTS (
                              SELECT 1 FROM quiz_attempts ungraded
                              WHERE ungraded.attempt_id = ANY(%s::uuid[])
                                AND ungraded.grading_status <> 'GRADED'
                                AND ungraded.deleted_at IS NULL
                          );
                        """,
                        (ids, ids),
                    )
                    released = cur.rowcount
                conn.commit()
            return Ok(released)
        except Exception as exc:
            return Err(MetadataStoreError(f"release_graded_attempts failed: {exc}"))

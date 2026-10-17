from __future__ import annotations

from document_chunk.adapters.metadata.postgres_repository_utils import (
    PostgresRepositoryBase,
    stable_uuid,
)
from document_chunk.domain.exceptions import MetadataStoreError
from document_chunk.domain.ports.course_knowledge_store import (
    ConceptRelation,
    CourseDocumentInfo,
    ICourseKnowledgeStore,
    KnowledgeChunk,
    NewRelation,
)
from document_chunk.shared.result import Err, Ok, Result

# Số đỉnh khởi đầu tối đa khi khớp một khái niệm: đủ để bắt các cách gọi khác
# nhau của cùng một ý mà không kéo cả đồ thị về.
_MAX_SEED_NODES = 5

_CHUNK_COLUMNS = """c.chunk_id::text, c.document_id::text, c.sort_order,
                    c.heading_path, c.page_number, c.content"""


def _chunk(row) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=row[0],
        document_id=row[1],
        sort_order=row[2],
        heading_path=tuple(row[3] or ()),
        page_number=row[4],
        content=row[5] or "",
    )


class PostgresCourseKnowledgeRepository(PostgresRepositoryBase, ICourseKnowledgeStore):
    """Đọc tài liệu/đoạn theo khóa học và đọc, ghi đồ thị quan hệ."""

    # ------------------------------------------------------------------ đọc

    def list_course_documents(
        self, course_id: str, chapter_code: str | None = None
    ) -> Result[list[CourseDocumentInfo], Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    course_uuid = self._resolve_course(cur, course_id)
                    cur.execute(
                        """
                        SELECT d.document_id::text, d.title, d.role, d.chapter_code,
                               COALESCE(MAX(c.page_number), 0), COUNT(c.chunk_id)
                        FROM documents d
                        JOIN chunks c ON c.document_id = d.document_id
                                     AND c.deleted_at IS NULL
                        WHERE d.course_id = %s::uuid
                          AND d.deleted_at IS NULL
                          AND (%s::text IS NULL OR d.chapter_code = %s::text)
                        GROUP BY d.document_id, d.title, d.role, d.chapter_code
                        ORDER BY d.chapter_code NULLS LAST, d.title;
                        """,
                        (course_uuid, chapter_code, chapter_code),
                    )
                    rows = cur.fetchall()
            return Ok(
                [
                    CourseDocumentInfo(
                        document_id=r[0],
                        title=r[1],
                        role=r[2],
                        chapter_code=r[3],
                        page_count=int(r[4] or 0),
                        chunk_count=int(r[5] or 0),
                    )
                    for r in rows
                ]
            )
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def list_page_chunks(
        self, document_id: str, page_number: int
    ) -> Result[list[KnowledgeChunk], Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT {_CHUNK_COLUMNS}
                        FROM chunks c
                        JOIN documents d ON d.document_id = c.document_id
                        WHERE c.document_id = %s::uuid
                          AND c.page_number = %s
                          AND c.deleted_at IS NULL
                          AND d.deleted_at IS NULL
                        ORDER BY c.sort_order;
                        """,
                        (stable_uuid(document_id), page_number),
                    )
                    return Ok([_chunk(r) for r in cur.fetchall()])
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def get_neighbor_chunks(
        self, chunk_id: str, window: int
    ) -> Result[list[KnowledgeChunk], Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT c.document_id::text, c.sort_order
                        FROM chunks c
                        JOIN documents d ON d.document_id = c.document_id
                        WHERE c.chunk_id = %s::uuid
                          AND c.deleted_at IS NULL
                          AND d.deleted_at IS NULL;
                        """,
                        (stable_uuid(chunk_id),),
                    )
                    anchor = cur.fetchone()
                    if anchor is None:
                        return Ok([])
                    cur.execute(
                        f"""
                        SELECT {_CHUNK_COLUMNS}
                        FROM chunks c
                        WHERE c.document_id = %s::uuid
                          AND c.deleted_at IS NULL
                          AND c.sort_order BETWEEN %s AND %s
                        ORDER BY c.sort_order;
                        """,
                        (anchor[0], anchor[1] - window, anchor[1] + window),
                    )
                    return Ok([_chunk(r) for r in cur.fetchall()])
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def find_relations(
        self, course_id: str, concept_key: str, hops: int, limit: int
    ) -> Result[list[ConceptRelation], Exception]:
        key = concept_key.strip()
        if not key:
            return Ok([])
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    course_uuid = self._resolve_course(cur, course_id)
                    seeds = self._seed_nodes(cur, course_uuid, key)
                    if not seeds:
                        return Ok([])

                    seen: dict[str, ConceptRelation] = {}
                    frontier = set(seeds)
                    visited_nodes = set(seeds)
                    for hop in range(1, max(1, hops) + 1):
                        if not frontier or len(seen) >= limit:
                            break
                        edges = self._edges_touching(cur, course_uuid, sorted(frontier), hop)
                        next_frontier: set[str] = set()
                        for edge, source_key, target_key in edges:
                            if edge.relation_id in seen:
                                continue
                            seen[edge.relation_id] = edge
                            for node in (source_key, target_key):
                                if node not in visited_nodes:
                                    next_frontier.add(node)
                            if len(seen) >= limit:
                                break
                        visited_nodes |= next_frontier
                        frontier = next_frontier
                    return Ok(list(seen.values())[:limit])
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    # ------------------------------------------------------------------ ghi

    def list_document_chunks(
        self, document_id: str
    ) -> Result[list[KnowledgeChunk], Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT {_CHUNK_COLUMNS}
                        FROM chunks c
                        JOIN documents d ON d.document_id = c.document_id
                        WHERE c.document_id = %s::uuid
                          AND c.deleted_at IS NULL
                          AND d.deleted_at IS NULL
                        ORDER BY c.sort_order;
                        """,
                        (stable_uuid(document_id),),
                    )
                    return Ok([_chunk(r) for r in cur.fetchall()])
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def replace_relations(
        self,
        *,
        course_id: str,
        chunk_ids: list[str],
        extraction_version: str,
        model: str | None,
        relations: list[NewRelation],
    ) -> Result[int, Exception]:
        if not chunk_ids:
            return Ok(0)
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    course_uuid = self._resolve_course(cur, course_id)
                    cur.execute(
                        "DELETE FROM knowledge_relations WHERE chunk_id = ANY(%s::uuid[]);",
                        (list(chunk_ids),),
                    )
                    written = 0
                    for rel in relations:
                        cur.execute(
                            """
                            INSERT INTO knowledge_relations (
                                course_id, document_id, chunk_id,
                                source_label, source_key, relation_type,
                                target_label, target_key, evidence_quote,
                                page_number, extraction_version, model
                            )
                            VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING;
                            """,
                            (
                                course_uuid,
                                rel.document_id,
                                rel.chunk_id,
                                rel.source_label,
                                rel.source_key,
                                rel.relation_type,
                                rel.target_label,
                                rel.target_key,
                                rel.evidence_quote,
                                rel.page_number,
                                extraction_version,
                                model,
                            ),
                        )
                        written += cur.rowcount or 0
                conn.commit()
            return Ok(written)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    # ------------------------------------------------------------- nội bộ

    @staticmethod
    def _resolve_course(cur, course_ref: str) -> str:
        """``course_ref`` là mã môn hoặc UUID; cùng cách giải quyết như repository đề cương."""
        cur.execute(
            "SELECT course_id::text FROM courses "
            "WHERE code = %s OR course_id = %s::uuid LIMIT 1;",
            (course_ref, stable_uuid(course_ref)),
        )
        row = cur.fetchone()
        return row[0] if row else stable_uuid(course_ref)

    @staticmethod
    def _seed_nodes(cur, course_uuid: str, key: str) -> list[str]:
        """Khóa nút khớp khái niệm: bằng nhau, chứa nhau (hai chiều, từ 4 ký tự), nhiều cạnh trước."""
        cur.execute(
            """
            SELECT k, COUNT(*) AS n FROM (
                SELECT kr.source_key AS k
                FROM knowledge_relations kr
                JOIN chunks c ON c.chunk_id = kr.chunk_id AND c.deleted_at IS NULL
                WHERE kr.course_id = %s::uuid
                UNION ALL
                SELECT kr.target_key
                FROM knowledge_relations kr
                JOIN chunks c ON c.chunk_id = kr.chunk_id AND c.deleted_at IS NULL
                WHERE kr.course_id = %s::uuid
            ) nodes
            WHERE k = %s
               OR k LIKE '%%' || %s || '%%'
               OR (length(k) >= 4 AND position(k in %s) > 0)
            GROUP BY k
            ORDER BY (k = %s) DESC, n DESC
            LIMIT %s;
            """,
            (course_uuid, course_uuid, key, key, key, key, _MAX_SEED_NODES),
        )
        return [r[0] for r in cur.fetchall()]

    @staticmethod
    def _edges_touching(
        cur, course_uuid: str, node_keys: list[str], hop: int
    ) -> list[tuple[ConceptRelation, str, str]]:
        cur.execute(
            """
            SELECT kr.relation_id::text, kr.source_label, kr.relation_type, kr.target_label,
                   kr.evidence_quote, kr.chunk_id::text, kr.document_id::text,
                   kr.page_number, kr.source_key, kr.target_key
            FROM knowledge_relations kr
            JOIN chunks c ON c.chunk_id = kr.chunk_id AND c.deleted_at IS NULL
            JOIN documents d ON d.document_id = kr.document_id AND d.deleted_at IS NULL
            WHERE kr.course_id = %s::uuid
              AND (kr.source_key = ANY(%s) OR kr.target_key = ANY(%s))
            ORDER BY kr.created_at, kr.relation_id;
            """,
            (course_uuid, node_keys, node_keys),
        )
        return [
            (
                ConceptRelation(
                    relation_id=r[0],
                    source_label=r[1],
                    relation_type=r[2],
                    target_label=r[3],
                    evidence_quote=r[4],
                    chunk_id=r[5],
                    document_id=r[6],
                    page_number=r[7],
                    hop=hop,
                ),
                r[8],
                r[9],
            )
            for r in cur.fetchall()
        ]


__all__ = ["PostgresCourseKnowledgeRepository"]

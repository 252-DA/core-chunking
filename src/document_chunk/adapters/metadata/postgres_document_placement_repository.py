"""Vai trò và chương của tài liệu — cột trên bảng `documents`."""
from document_chunk.adapters.metadata.postgres_repository_utils import PostgresRepositoryBase
from document_chunk.domain.exceptions import MetadataStoreError
from document_chunk.domain.ports.metadata_store import StoredDocumentPlacement
from document_chunk.shared.result import Err, Ok, Result


class PostgresDocumentPlacementRepository(PostgresRepositoryBase):
    def get_document_placement(
        self, document_id: str
    ) -> Result[StoredDocumentPlacement | None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT document_id::text, course_id::text, role, chapter_code,
                               chapter_provenance, chapter_confidence
                        FROM documents
                        WHERE document_id = %s::uuid;
                        """,
                        (document_id,),
                    )
                    row = cur.fetchone()
            if row is None:
                return Ok(None)
            return Ok(StoredDocumentPlacement(
                document_id=row[0],
                course_id=row[1],
                role=row[2] or "lecture",
                chapter_code=row[3],
                chapter_provenance=row[4],
                chapter_confidence=float(row[5]) if row[5] is not None else None,
            ))
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def set_document_chapter(
        self,
        document_id: str,
        chapter_code: str,
        provenance: str,
        confidence: float,
        reason: str,
    ) -> Result[bool, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE documents
                           SET chapter_code = %s,
                               chapter_provenance = %s,
                               chapter_confidence = %s,
                               chapter_reason = %s
                         WHERE document_id = %s::uuid
                           AND chapter_provenance IS DISTINCT FROM 'confirmed';
                        """,
                        (chapter_code, provenance, round(confidence, 2), reason[:255], document_id),
                    )
                    updated = cur.rowcount > 0
                conn.commit()
            return Ok(updated)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

    def delete_inferred_chunk_lo_mappings(self, document_id: str) -> Result[int, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM chunk_lo_mappings clm
                         USING chunks c
                         WHERE c.chunk_id = clm.chunk_id
                           AND c.document_id = %s::uuid
                           AND clm.provenance <> 'confirmed';
                        """,
                        (document_id,),
                    )
                    deleted = cur.rowcount
                conn.commit()
            return Ok(deleted)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL metadata store operation failed", cause=exc))

#!/usr/bin/env python
"""
Backfill `chunk_lo_mappings` cho các tài liệu đã index trước khi mapper được nối
vào pipeline.

`MapChunksToLosUseCase` trước đây chỉ được khai báo trong container mà không nơi
nào gọi, nên bảng rỗng và mọi thứ dựng trên nó đều hỏng lặng lẽ: sinh quiz theo
LO báo "No grounded source chunks", enrichment sinh xong rồi INSERT ra 0 dòng.
Từ nay `RunPipelineUseCase` gọi mapper sau mỗi lần index; script này xử lý phần
tồn đọng.

    python scripts/backfill_chunk_lo_mappings.py --dry-run
    python scripts/backfill_chunk_lo_mappings.py --course CO3011

Chỉ ghi cạnh có provenance 'inferred'. Cạnh giảng viên đã xác nhận không bị hạ
cấp (xem ON CONFLICT trong upsert_chunk_lo_mappings).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from document_chunk.adapters.curriculum.heuristic_lo_mapper import HeuristicLoMapper
from document_chunk.adapters.graph.noop_graph_store import NoopGraphStore
from document_chunk.adapters.metadata.postgres_metadata_store import PostgresMetadataStore
from document_chunk.application.use_cases.map_chunks_to_los import (
    MapChunksToLosRequest,
    MapChunksToLosUseCase,
)
from document_chunk.infrastructure.config import Settings


def _documents(store: PostgresMetadataStore, course_code: str | None) -> list[tuple[str, str]]:
    """(document_id, course_id) của các tài liệu đã index và có gắn học phần."""
    sql = """
        SELECT d.document_id::text, d.course_id::text
        FROM documents d
        JOIN courses c ON c.course_id = d.course_id
        WHERE d.deleted_at IS NULL
          AND d.course_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM chunks ch
                       WHERE ch.document_id = d.document_id AND ch.deleted_at IS NULL)
    """
    params: tuple = ()
    if course_code:
        sql += " AND c.code = %s"
        params = (course_code,)
    sql += " ORDER BY d.created_at;"

    with store._connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [(r[0], r[1]) for r in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course", help="chỉ chạy cho một mã học phần, ví dụ CO3011")
    parser.add_argument("--dry-run", action="store_true", help="chỉ đếm, không ghi")
    args = parser.parse_args()

    settings = Settings()
    store = PostgresMetadataStore(settings.sql)
    try:
        documents = _documents(store, args.course)
        if not documents:
            print("Không có tài liệu nào cần backfill.")
            return 0

        use_case = MapChunksToLosUseCase(
            metadata_store=store,
            graph_store=NoopGraphStore(),
            lo_mapper=HeuristicLoMapper(),
        )

        total = 0
        failed = 0
        for document_id, course_id in documents:
            if args.dry_run:
                print(f"[dry-run] {document_id}  course={course_id}")
                continue
            result = use_case.execute(
                MapChunksToLosRequest(document_id=document_id, course_id=course_id)
            )
            if result.is_err():
                failed += 1
                print(f"  LỖI {document_id}: {result.error}")
                continue
            count = result.unwrap().mapping_count
            total += count
            print(f"  {document_id}: {count} cạnh")

        print(
            f"\nTài liệu: {len(documents)} | cạnh đã ghi: {total} | lỗi: {failed}"
            + ("  (dry-run)" if args.dry_run else "")
        )
        return 1 if failed else 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import uuid
from typing import Any

from document_chunk.adapters.metadata.postgres_repository_utils import (
    PostgresRepositoryBase,
)
from document_chunk.domain.exceptions import MetadataStoreError
from document_chunk.domain.outbox_events import OutboxEventType
from document_chunk.shared.result import Err, Ok, Result


class PostgresOutboxRepository(PostgresRepositoryBase):
    """Transactional outbox write operations shared by metadata repositories."""

    def append_outbox_event(
        self,
        event_type: OutboxEventType,
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

    def _delete_pending_outbox_cursor(
        self,
        cur: Any,
        aggregate_id: str,
        event_type: OutboxEventType | None,
    ) -> None:
        if event_type is None:
            return

        cur.execute(
            """
            DELETE FROM outbox_events
            WHERE aggregate_id = %s::uuid
              AND UPPER(event_type) = %s
              AND status = 'PENDING';
            """,
            (aggregate_id, OutboxEventType.normalize(event_type).value),
        )

    def _append_outbox_event_cursor(
        self,
        cur: Any,
        event_id: str,
        event_type: OutboxEventType,
        aggregate_id: str,
        payload: dict,
    ) -> None:
        cur.execute(
            """
            INSERT INTO outbox_events (
                event_id,
                event_type,
                aggregate_type,
                aggregate_id,
                payload,
                status,
                retry_count,
                last_error
            )
            VALUES (%s::uuid, %s, 'document', %s::uuid, %s::jsonb, 'PENDING', 0, NULL);
            """,
            (
                event_id,
                OutboxEventType.normalize(event_type).value,
                aggregate_id,
                json.dumps(payload),
            ),
        )

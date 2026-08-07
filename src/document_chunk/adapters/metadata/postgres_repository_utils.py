from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from typing import Any

from document_chunk.domain.outbox_events import OutboxEventType


def stable_uuid(value: str) -> str:
    """Return a UUID string while preserving valid UUID inputs."""

    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(value)))


class PostgresRepositoryBase:
    """Connection and outbox hooks supplied by the composed store facade."""

    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def _delete_pending_outbox_cursor(
        self,
        cur: Any,
        aggregate_id: str,
        event_type: OutboxEventType | None,
    ) -> None:
        raise NotImplementedError

    def _append_outbox_event_cursor(
        self,
        cur: Any,
        event_id: str,
        event_type: OutboxEventType,
        aggregate_id: str,
        payload: dict,
    ) -> None:
        raise NotImplementedError

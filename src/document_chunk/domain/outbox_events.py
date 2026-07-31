"""Canonical contracts for events relayed from the PostgreSQL outbox."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class OutboxEventType(str, Enum):
    """Canonical event names stored in PostgreSQL and sent through BullMQ."""

    HEADING_GRAPH_PROJECT = "HEADING_GRAPH_PROJECT"
    CONCEPT_GRAPH_PROJECT = "CONCEPT_GRAPH_PROJECT"
    DOCUMENT_DELETED = "DOCUMENT_DELETED"
    CONTENT_GENERATION_REQUESTED = "CONTENT_GENERATION_REQUESTED"
    LESSON_PUBLISHED = "LESSON_PUBLISHED"

    @classmethod
    def normalize(cls, value: "OutboxEventType | str") -> "OutboxEventType":
        """Return the canonical enum, accepting legacy lowercase wire values."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValueError("outbox event type must be a string")

        normalized = value.strip().upper()
        try:
            return cls(normalized)
        except ValueError as exc:
            raise ValueError(f"unsupported outbox event type: {value!r}") from exc


def normalize_outbox_event_type(value: OutboxEventType | str) -> OutboxEventType:
    """Normalize canonical and legacy event names through one public helper."""

    return OutboxEventType.normalize(value)


@dataclass(frozen=True)
class OutboxRelayEvent:
    """Validated BullMQ payload produced by the Core outbox relay."""

    event_id: str
    event_type: OutboxEventType
    aggregate_type: str | None
    aggregate_id: str | None
    payload: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OutboxRelayEvent":
        if not isinstance(value, Mapping):
            raise ValueError("outbox relay payload must be an object")

        event_id = cls._required_string(value, "event_id")
        event_type = normalize_outbox_event_type(cls._required_string(value, "event_type"))
        aggregate_type = cls._optional_string(value, "aggregate_type")
        aggregate_id = cls._optional_string(value, "aggregate_id")
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("outbox relay field 'payload' must be an object")

        return cls(
            event_id=event_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=dict(payload),
        )

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> "OutboxRelayEvent":
        return cls.from_mapping(value)

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "OutboxRelayEvent":
        """Named constructor matching BullMQ's job-data terminology."""

        return cls.from_mapping(value)

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "payload": dict(self.payload),
        }

    @staticmethod
    def _required_string(value: Mapping[str, Any], field_name: str) -> str:
        field_value = value.get(field_name)
        if not isinstance(field_value, str) or not field_value.strip():
            raise ValueError(f"outbox relay field {field_name!r} must be a non-empty string")
        return field_value.strip()

    @staticmethod
    def _optional_string(value: Mapping[str, Any], field_name: str) -> str | None:
        field_value = value.get(field_name)
        if field_value is None:
            return None
        if not isinstance(field_value, str) or not field_value.strip():
            raise ValueError(
                f"outbox relay field {field_name!r} must be a non-empty string or null"
            )
        return field_value.strip()

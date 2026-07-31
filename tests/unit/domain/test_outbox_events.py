import pytest

from document_chunk.domain.outbox_events import (
    OutboxEventType,
    OutboxRelayEvent,
    normalize_outbox_event_type,
)
from document_chunk.domain.ports.job_queue import OUTBOX_RELAY_QUEUE_NAME


@pytest.mark.parametrize("event_type", list(OutboxEventType))
def test_normalize_outbox_event_type_accepts_canonical_and_legacy_values(event_type):
    assert normalize_outbox_event_type(event_type) is event_type
    assert normalize_outbox_event_type(event_type.value) is event_type
    assert normalize_outbox_event_type(event_type.value.lower()) is event_type


def test_normalize_outbox_event_type_rejects_unknown_value():
    with pytest.raises(ValueError, match="unsupported outbox event type"):
        normalize_outbox_event_type("UNKNOWN_EVENT")


def test_outbox_relay_event_parses_legacy_job_payload_and_serializes_canonical_value():
    event = OutboxRelayEvent.from_mapping(
        {
            "event_id": "event-001",
            "event_type": "document_deleted",
            "aggregate_type": "document",
            "aggregate_id": "doc-001",
            "payload": {"document_id": "doc-001"},
        }
    )

    assert event.event_type is OutboxEventType.DOCUMENT_DELETED
    assert event.to_payload() == {
        "event_id": "event-001",
        "event_type": "DOCUMENT_DELETED",
        "aggregate_type": "document",
        "aggregate_id": "doc-001",
        "payload": {"document_id": "doc-001"},
    }


def test_outbox_relay_event_rejects_non_object_business_payload():
    with pytest.raises(ValueError, match="field 'payload' must be an object"):
        OutboxRelayEvent.from_mapping(
            {
                "event_id": "event-001",
                "event_type": "DOCUMENT_DELETED",
                "aggregate_type": "document",
                "aggregate_id": "doc-001",
                "payload": "doc-001",
            }
        )


def test_outbox_relay_queue_name_is_shared_contract():
    assert OUTBOX_RELAY_QUEUE_NAME == "outbox_relay"

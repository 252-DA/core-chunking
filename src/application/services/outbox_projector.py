from src.domain.ports.graph_store import GraphChunk, IGraphStore
from src.domain.ports.metadata_store import IMetadataStore, OutboxEvent
from src.shared.logger import get_logger
from src.shared.result import Err, Ok, Result

logger = get_logger(__name__)

_EVENT_HEADING_GRAPH_PROJECT = "heading_graph_project"


class OutboxProjector:
    def __init__(self, metadata_store: IMetadataStore, graph_store: IGraphStore) -> None:
        self._metadata_store = metadata_store
        self._graph_store = graph_store

    def run_once(self, limit: int = 100) -> Result[int, Exception]:
        pending_result = self._metadata_store.fetch_pending_outbox(limit=limit)
        if pending_result.is_err():
            return pending_result

        processed = 0
        for event in pending_result.unwrap():
            result = self._process_event(event)
            if result.is_err():
                logger.error("outbox_projector.event_failed", event_id=event.id, error=str(result.error))
            else:
                processed += 1

        return Ok(processed)

    def _process_event(self, event: OutboxEvent) -> Result[None, Exception]:
        if event.event_type != _EVENT_HEADING_GRAPH_PROJECT:
            done_result = self._metadata_store.mark_outbox_done(event.id)
            if done_result.is_err():
                return done_result
            return Ok(None)

        payload = event.payload
        chunks_payload = payload.get("chunks", [])
        chunks = [
            GraphChunk(
                chunk_id=item["chunk_id"],
                chunk_index=item["chunk_index"],
                heading_path=tuple(item.get("heading_path", [])),
            )
            for item in chunks_payload
        ]

        upsert_result = self._graph_store.upsert_heading_graph(
            document_id=payload["document_id"],
            course_id=payload.get("course_id"),
            owner_id=payload.get("owner_id"),
            chunks=chunks,
        )
        if upsert_result.is_err():
            fail_result = self._metadata_store.mark_outbox_failed(
                event.id,
                str(upsert_result.error),
            )
            if fail_result.is_err():
                return fail_result
            return Err(upsert_result.error)

        done_result = self._metadata_store.mark_outbox_done(event.id)
        if done_result.is_err():
            return done_result
        return Ok(None)

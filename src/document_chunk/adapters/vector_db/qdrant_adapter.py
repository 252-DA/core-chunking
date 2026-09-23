"""
QdrantAdapter — implement IVectorStore dùng Qdrant.

Design:
  - Mỗi Chunk → 1 Qdrant point (id=chunk.id, vector, payload)
  - Payload chứa toàn bộ metadata + content để reconstruct Chunk khi search
  - Collection tự tạo nếu chưa tồn tại
  - SearchFilter → Qdrant filter conditions
"""
import hashlib
import time
import uuid
from functools import cached_property

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from document_chunk.domain.entities.chunk import Chunk, ChunkMetadata
from document_chunk.domain.entities.document import DocumentType
from document_chunk.domain.entities.embedding import Embedding
from document_chunk.domain.entities.search import SearchFilter, SearchResult
from document_chunk.domain.exceptions import VectorStoreError
from document_chunk.domain.ports.vector_store import IVectorStore
from document_chunk.infrastructure.config import QdrantConfig
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

# Payload keys — dùng constants tránh typo
_F_CHUNK_ID       = "chunk_id"
_F_DOCUMENT_ID    = "document_id"
_F_DOCUMENT_NAME  = "document_name"
_F_COURSE_ID      = "course_id"
_F_OWNER_ID       = "owner_id"
_F_DOC_TYPE       = "doc_type"
_F_CHUNK_INDEX    = "chunk_index"
_F_HEADING_PATH   = "heading_path"
_F_HEADING_LEVEL  = "heading_level"
_F_PAGE_NUMBER    = "page_number"
_F_LANGUAGE       = "language"
_F_CONTENT        = "content"
_F_CONTENT_HASH   = "content_hash"
_F_IMAGES         = "images"
_F_EMBEDDING_INPUT = "embedding_input"


class QdrantAdapter(IVectorStore):
    def __init__(self, config: QdrantConfig) -> None:
        self._config = config

    @cached_property
    def _client(self) -> QdrantClient:
        """Lazy connect — chỉ kết nối khi cần."""
        client = QdrantClient(
            host=self._config.host,
            port=self._config.port,
            api_key=self._config.api_key,
            timeout=30,
        )
        logger.info(
            "qdrant.connected",
            host=self._config.host,
            port=self._config.port,
        )
        self._ensure_collection(client)
        return client

    # ------------------------------------------------------------------
    # IVectorStore implementation
    # ------------------------------------------------------------------

    def upsert(
        self, chunks: list[Chunk], embeddings: list[Embedding]
    ) -> Result[None, Exception]:
        if not chunks:
            return Ok(None)

        with tracer.start_as_current_span("qdrant.upsert") as span:
            span.set_attribute("count", len(chunks))
            span.set_attribute("collection", self._config.collection_name)

            try:
                points = [
                    qmodels.PointStruct(
                        id=str(uuid.UUID(chunk.id)),  # Qdrant cần valid UUID
                        vector=list(embedding.vector),
                        payload=self._chunk_to_payload(chunk),
                    )
                    for chunk, embedding in zip(chunks, embeddings)
                ]

                self._client.upsert(
                    collection_name=self._config.collection_name,
                    points=points,
                    wait=True,
                )

                logger.info(
                    "qdrant.upserted",
                    count=len(points),
                    collection=self._config.collection_name,
                )
                return Ok(None)

            except Exception as e:
                logger.error("qdrant.upsert.failed", error=str(e))
                return Err(VectorStoreError("Qdrant upsert failed", cause=e))

    def search(
        self,
        query_vector: list[float],
        top_k: int,
        score_threshold: float = 0.0,
        filters: SearchFilter | None = None,
    ) -> Result[list[SearchResult], Exception]:
        with tracer.start_as_current_span("qdrant.search") as span:
            span.set_attribute("top_k", top_k)
            span.set_attribute("score_threshold", score_threshold)

            try:
                t0 = time.perf_counter()

                qdrant_filter = self._build_filter(filters or SearchFilter())

                hits = self._client.query_points(
                    collection_name=self._config.collection_name,
                    query=query_vector,
                    limit=top_k,
                    score_threshold=score_threshold if score_threshold > 0 else None,
                    query_filter=qdrant_filter,
                    with_payload=True,
                ).points

                results = [
                    SearchResult(
                        chunk=self._payload_to_chunk(hit.payload),
                        score=hit.score,
                        rank=idx + 1,
                    )
                    for idx, hit in enumerate(hits)
                    if hit.payload
                ]

                elapsed_ms = (time.perf_counter() - t0) * 1000
                logger.info(
                    "qdrant.search.done",
                    found=len(results),
                    duration_ms=round(elapsed_ms, 1),
                )
                return Ok(results)

            except Exception as e:
                logger.error("qdrant.search.failed", error=str(e))
                return Err(VectorStoreError("Qdrant search failed", cause=e))

    def delete(self, chunk_ids: list[str]) -> Result[None, Exception]:
        try:
            self._client.delete(
                collection_name=self._config.collection_name,
                points_selector=qmodels.PointIdsList(
                    points=[str(uuid.UUID(cid)) for cid in chunk_ids]
                ),
                wait=True,
            )
            logger.info("qdrant.deleted", count=len(chunk_ids))
            return Ok(None)
        except Exception as e:
            logger.error("qdrant.delete.failed", error=str(e))
            return Err(VectorStoreError("Qdrant delete failed", cause=e))

    def delete_by_document(self, document_id: str) -> Result[None, Exception]:
        try:
            self._client.delete(
                collection_name=self._config.collection_name,
                points_selector=qmodels.FilterSelector(
                    filter=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(
                                key=_F_DOCUMENT_ID,
                                match=qmodels.MatchValue(value=document_id),
                            )
                        ]
                    )
                ),
                wait=True,
            )
            logger.info("qdrant.deleted_by_document", document_id=document_id)
            return Ok(None)
        except Exception as e:
            logger.error("qdrant.delete_by_document.failed", error=str(e))
            return Err(VectorStoreError("Qdrant delete_by_document failed", cause=e))

    def delete_stale(self, document_id: str, keep_ids: list[str]) -> Result[None, Exception]:
        """Replace a document snapshot only after its new vectors were upserted."""
        try:
            self._client.delete(
                collection_name=self._config.collection_name,
                points_selector=qmodels.FilterSelector(filter=qmodels.Filter(
                    must=[qmodels.FieldCondition(key=_F_DOCUMENT_ID, match=qmodels.MatchValue(value=document_id))],
                    must_not=[qmodels.HasIdCondition(has_id=keep_ids)] if keep_ids else [],
                )), wait=True,
            )
            return Ok(None)
        except Exception as exc:
            return Err(VectorStoreError("Qdrant stale vector cleanup failed", cause=exc))

    def count(self) -> Result[int, Exception]:
        try:
            result = self._client.count(
                collection_name=self._config.collection_name,
                exact=True,
            )
            return Ok(result.count)
        except Exception as e:
            logger.error("qdrant.count.failed", error=str(e))
            return Err(VectorStoreError("Qdrant count failed", cause=e))

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    def _ensure_collection(self, client: QdrantClient) -> None:
        """Tạo collection nếu chưa tồn tại."""
        name = self._config.collection_name
        collections = [c.name for c in client.get_collections().collections]

        if name not in collections:
            client.create_collection(
                collection_name=name,
                vectors_config=qmodels.VectorParams(
                    size=self._config.vector_size,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            # Index các payload fields hay filter
            for field in [_F_DOCUMENT_ID, _F_DOC_TYPE, _F_LANGUAGE, _F_COURSE_ID, _F_OWNER_ID]:
                client.create_payload_index(
                    collection_name=name,
                    field_name=field,
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                )
            logger.info("qdrant.collection.created", name=name)
        else:
            logger.info("qdrant.collection.exists", name=name)

        for field in ("content_type", "chunker_version"):
            client.create_payload_index(collection_name=name, field_name=field,
                                        field_schema=qmodels.PayloadSchemaType.KEYWORD)

    # ------------------------------------------------------------------
    # Payload helpers
    # ------------------------------------------------------------------

    def _chunk_to_payload(self, chunk: Chunk) -> dict:
        """Chunk → Qdrant payload dict."""
        return {
            "content_type": chunk.metadata.content_type,
            "token_count": chunk.metadata.token_count,
            "char_count": chunk.metadata.char_count,
            "page_start": chunk.metadata.page_start,
            "page_end": chunk.metadata.page_end,
            "section_id": chunk.metadata.section_id,
            "part_index": chunk.metadata.part_index,
            "part_count": chunk.metadata.part_count,
            "chunker_version": chunk.metadata.chunker_version,
            "importance_score": chunk.metadata.importance_score,
            _F_CHUNK_ID:      chunk.id,
            _F_DOCUMENT_ID:   chunk.metadata.document_id,
            _F_DOCUMENT_NAME: chunk.metadata.document_name,
            _F_COURSE_ID:     chunk.metadata.course_id,
            _F_OWNER_ID:      chunk.metadata.owner_id,
            _F_DOC_TYPE:      chunk.metadata.document_type.value,
            _F_CHUNK_INDEX:   chunk.metadata.chunk_index,
            _F_HEADING_PATH:  list(chunk.metadata.heading_path),
            _F_HEADING_LEVEL: chunk.metadata.heading_level,
            _F_PAGE_NUMBER:   chunk.metadata.page_number,
            _F_LANGUAGE:      chunk.metadata.language,
            _F_CONTENT:       chunk.content,
            _F_CONTENT_HASH:  chunk.content_hash,
            _F_IMAGES:        chunk.images,
            _F_EMBEDDING_INPUT: chunk.embedding_input,
        }

    def _payload_to_chunk(self, payload: dict) -> Chunk:
        """Qdrant payload → Chunk (reconstruct từ stored data)."""
        content = payload[_F_CONTENT]
        metadata = ChunkMetadata(
            content_type=payload.get("content_type"),
            token_count=payload.get("token_count"),
            char_count=payload.get("char_count"),
            page_start=payload.get("page_start"),
            page_end=payload.get("page_end"),
            section_id=payload.get("section_id"),
            part_index=payload.get("part_index"),
            part_count=payload.get("part_count"),
            chunker_version=payload.get("chunker_version"),
            importance_score=payload.get("importance_score"),
            document_id=payload[_F_DOCUMENT_ID],
            document_name=payload[_F_DOCUMENT_NAME],
            document_type=DocumentType(payload[_F_DOC_TYPE]),
            chunk_index=payload[_F_CHUNK_INDEX],
            heading_path=tuple(payload.get(_F_HEADING_PATH, [])),
            heading_level=payload.get(_F_HEADING_LEVEL, 0),
            page_number=payload.get(_F_PAGE_NUMBER),
            language=payload.get(_F_LANGUAGE),
            course_id=payload.get(_F_COURSE_ID),
            owner_id=payload.get(_F_OWNER_ID),
        )
        return Chunk(
            id=payload[_F_CHUNK_ID],
            content=content,
            embedding_input=payload.get(_F_EMBEDDING_INPUT) or content,
            content_hash=payload.get(_F_CONTENT_HASH) or hashlib.md5(content.encode()).hexdigest(),
            metadata=metadata,
            images=payload.get(_F_IMAGES, []),
        )

    # ------------------------------------------------------------------
    # Filter builder
    # ------------------------------------------------------------------

    def _build_filter(self, filters: SearchFilter) -> qmodels.Filter | None:
        """SearchFilter → Qdrant Filter conditions."""
        conditions: list[qmodels.Condition] = []

        if filters.doc_types:
            conditions.append(qmodels.FieldCondition(
                key=_F_DOC_TYPE,
                match=qmodels.MatchAny(
                    any=[dt.value for dt in filters.doc_types]
                ),
            ))

        if filters.document_ids:
            conditions.append(qmodels.FieldCondition(
                key=_F_DOCUMENT_ID,
                match=qmodels.MatchAny(any=list(filters.document_ids)),
            ))

        if filters.language:
            conditions.append(qmodels.FieldCondition(
                key=_F_LANGUAGE,
                match=qmodels.MatchValue(value=filters.language),
            ))

        if filters.course_id:
            conditions.append(qmodels.FieldCondition(
                key=_F_COURSE_ID,
                match=qmodels.MatchValue(value=filters.course_id),
            ))

        if filters.owner_id:
            conditions.append(qmodels.FieldCondition(
                key=_F_OWNER_ID,
                match=qmodels.MatchValue(value=filters.owner_id),
            ))

        excluded = [qmodels.FieldCondition(
            key="content_type", match=qmodels.MatchAny(any=list(filters.exclude_content_types)),
        )] if filters.exclude_content_types else []
        return qmodels.Filter(must=conditions, must_not=excluded) if conditions or excluded else None

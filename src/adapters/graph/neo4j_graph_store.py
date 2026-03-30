from functools import cached_property

from src.domain.ports.graph_store import GraphChunk, IGraphStore
from src.infrastructure.config import Neo4jConfig
from src.shared.logger import get_logger
from src.shared.result import Err, Ok, Result

logger = get_logger(__name__)


class Neo4jGraphStore(IGraphStore):
    def __init__(self, config: Neo4jConfig) -> None:
        self._config = config

    @cached_property
    def _driver(self):
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise ImportError("neo4j driver not installed. Install dependency: neo4j") from exc

        return GraphDatabase.driver(
            self._config.uri,
            auth=(self._config.username, self._config.password),
        )

    def upsert_heading_graph(
        self,
        document_id: str,
        course_id: str | None,
        owner_id: str | None,
        chunks: list[GraphChunk],
    ) -> Result[None, Exception]:
        try:
            ordered = sorted(chunks, key=lambda c: c.chunk_index)
            with self._driver.session(database=self._config.database) as session:
                session.execute_write(self._merge_document, document_id, course_id, owner_id)

                prev_chunk_id: str | None = None
                for chunk in ordered:
                    session.execute_write(
                        self._merge_chunk,
                        document_id,
                        chunk.chunk_id,
                        chunk.chunk_index,
                    )
                    session.execute_write(
                        self._merge_heading_path,
                        document_id,
                        chunk.chunk_id,
                        list(chunk.heading_path),
                    )
                    if prev_chunk_id is not None:
                        session.execute_write(self._merge_next_edge, prev_chunk_id, chunk.chunk_id)
                    prev_chunk_id = chunk.chunk_id

            return Ok(None)
        except Exception as exc:
            logger.error("neo4j_graph_store.upsert_failed", document_id=document_id, error=str(exc))
            return Err(exc)

    def delete_document(self, document_id: str) -> Result[None, Exception]:
        try:
            with self._driver.session(database=self._config.database) as session:
                session.run(
                    """
                    MATCH (d:Document {id: $document_id})
                    OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
                    DETACH DELETE c
                    WITH d
                    OPTIONAL MATCH (d)-[:HAS_HEADING]->(h:Heading)
                    DETACH DELETE h
                    WITH d
                    DETACH DELETE d
                    """,
                    document_id=document_id,
                )
            return Ok(None)
        except Exception as exc:
            logger.error("neo4j_graph_store.delete_failed", document_id=document_id, error=str(exc))
            return Err(exc)

    @staticmethod
    def _merge_document(tx, document_id: str, course_id: str | None, owner_id: str | None) -> None:
        tx.run(
            """
            MERGE (d:Document {id: $document_id})
            SET d.updated_at = datetime()
            """,
            document_id=document_id,
        )

        if course_id:
            tx.run(
                """
                MERGE (c:Course {id: $course_id})
                MERGE (d:Document {id: $document_id})
                MERGE (c)-[:HAS_DOCUMENT]->(d)
                """,
                course_id=course_id,
                document_id=document_id,
            )

        if owner_id:
            tx.run(
                """
                MERGE (o:Owner {id: $owner_id})
                MERGE (d:Document {id: $document_id})
                MERGE (o)-[:OWNS_DOCUMENT]->(d)
                """,
                owner_id=owner_id,
                document_id=document_id,
            )

    @staticmethod
    def _merge_chunk(tx, document_id: str, chunk_id: str, chunk_index: int) -> None:
        tx.run(
            """
            MERGE (c:Chunk {id: $chunk_id})
            SET c.document_id = $document_id,
                c.chunk_index = $chunk_index,
                c.updated_at = datetime()
            WITH c
            MATCH (d:Document {id: $document_id})
            MERGE (d)-[:HAS_CHUNK]->(c)
            """,
            document_id=document_id,
            chunk_id=chunk_id,
            chunk_index=chunk_index,
        )

    @staticmethod
    def _merge_heading_path(tx, document_id: str, chunk_id: str, heading_path: list[str]) -> None:
        if not heading_path:
            return

        parent_heading_id: str | None = None
        for idx, title in enumerate(heading_path, start=1):
            heading_id = f"{document_id}:{idx}:{' > '.join(heading_path[:idx])}"
            tx.run(
                """
                MERGE (h:Heading {id: $heading_id})
                SET h.document_id = $document_id,
                    h.level = $level,
                    h.title = $title,
                    h.updated_at = datetime()
                """,
                heading_id=heading_id,
                document_id=document_id,
                level=idx,
                title=title,
            )

            if idx == 1:
                tx.run(
                    """
                    MATCH (d:Document {id: $document_id})
                    MATCH (h:Heading {id: $heading_id})
                    MERGE (d)-[:HAS_HEADING]->(h)
                    """,
                    document_id=document_id,
                    heading_id=heading_id,
                )

            if parent_heading_id is not None:
                tx.run(
                    """
                    MATCH (p:Heading {id: $parent_heading_id})
                    MATCH (h:Heading {id: $heading_id})
                    MERGE (p)-[:HAS_SUBHEADING]->(h)
                    """,
                    parent_heading_id=parent_heading_id,
                    heading_id=heading_id,
                )

            parent_heading_id = heading_id

        if parent_heading_id is not None:
            tx.run(
                """
                MATCH (h:Heading {id: $heading_id})
                MATCH (c:Chunk {id: $chunk_id})
                MERGE (h)-[:HAS_CHUNK]->(c)
                """,
                heading_id=parent_heading_id,
                chunk_id=chunk_id,
            )

    @staticmethod
    def _merge_next_edge(tx, previous_chunk_id: str, next_chunk_id: str) -> None:
        tx.run(
            """
            MATCH (a:Chunk {id: $previous_chunk_id})
            MATCH (b:Chunk {id: $next_chunk_id})
            MERGE (a)-[:NEXT]->(b)
            """,
            previous_chunk_id=previous_chunk_id,
            next_chunk_id=next_chunk_id,
        )

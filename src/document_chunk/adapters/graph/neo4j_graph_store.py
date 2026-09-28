from functools import cached_property

from document_chunk.domain.exceptions import GraphStoreError
from document_chunk.domain.ports.graph_store import (
    GraphAssessment,
    GraphChapter,
    GraphChapterLOEdge,
    GraphChunk,
    GraphChunkConcept,
    GraphChunkLOEdge,
    GraphConcept,
    GraphDocument,
    GraphLO,
    IGraphStore,
)
from document_chunk.infrastructure.config import Neo4jConfig
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)


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

    def close(self) -> None:
        driver = self.__dict__.get("_driver")
        if driver is None:
            return

        driver.close()
        logger.info("neo4j_graph_store.closed")

    def upsert_heading_graph(
        self,
        document: GraphDocument,
        course_id: str | None,
        owner_id: str | None,
        chunks: list[GraphChunk],
    ) -> Result[None, Exception]:
        with tracer.start_as_current_span("neo4j.upsert_heading_graph") as span:
            span.set_attribute("document.id", document.document_id)
            span.set_attribute("chunks.count", len(chunks))
            try:
                ordered = sorted(chunks, key=lambda c: c.chunk_index)
                projection = self._build_heading_projection(document.document_id, ordered)
                with self._driver.session(database=self._config.database) as session:
                    session.execute_write(
                        self._merge_heading_graph,
                        {
                            "id": document.document_id,
                            "name": document.document_name,
                            "doc_type": document.doc_type.value,
                        },
                        course_id,
                        owner_id,
                        projection["chunks"],
                        projection["headings"],
                        projection["root_heading_edges"],
                        projection["subheading_edges"],
                        projection["heading_chunk_edges"],
                        projection["next_edges"],
                    )

                return Ok(None)
            except Exception as exc:
                logger.error(
                    "neo4j_graph_store.upsert_failed",
                    document_id=document.document_id,
                    error=str(exc),
                )
                return Err(GraphStoreError("Neo4j graph store operation failed", cause=exc))

    def upsert_concept_graph(
        self,
        document_id: str,
        concepts: list[GraphConcept],
        mentions: list[GraphChunkConcept],
    ) -> Result[None, Exception]:
        with tracer.start_as_current_span("neo4j.upsert_concept_graph") as span:
            span.set_attribute("document.id", document_id)
            span.set_attribute("concepts.count", len(concepts))
            span.set_attribute("mentions.count", len(mentions))
            try:
                with self._driver.session(database=self._config.database) as session:
                    chunk_ids = sorted({mention.chunk_id for mention in mentions})
                    if chunk_ids:
                        existing_count = session.execute_read(self._count_chunks, chunk_ids)
                        if existing_count != len(chunk_ids):
                            raise ValueError(
                                "Cannot project concept graph before chunk nodes exist "
                                f"for document {document_id}"
                            )

                    if concepts:
                        session.execute_write(
                            self._merge_concepts,
                            [
                                {
                                    "id": concept.concept_id,
                                    "name": concept.name,
                                    "canonical_name": concept.canonical_name,
                                    "slug": concept.slug,
                                    "category": concept.category,
                                    "language": concept.language,
                                    "domain": concept.domain,
                                }
                                for concept in concepts
                            ],
                        )

                    if mentions:
                        session.execute_write(
                            self._merge_mentions,
                            [
                                {
                                    "chunk_id": mention.chunk_id,
                                    "concept_id": mention.concept_id,
                                    "confidence": mention.confidence,
                                    "source": mention.source,
                                }
                                for mention in mentions
                            ],
                        )

                return Ok(None)
            except Exception as exc:
                logger.error(
                    "neo4j_graph_store.upsert_concept_failed",
                    document_id=document_id,
                    error=str(exc),
                )
                return Err(GraphStoreError("Neo4j graph store operation failed", cause=exc))

    def delete_document(self, document_id: str) -> Result[None, Exception]:
        with tracer.start_as_current_span("neo4j.delete_document") as span:
            span.set_attribute("document.id", document_id)
            try:
                with self._driver.session(database=self._config.database) as session:
                    session.run(
                        """
                        MATCH (d:Document {id: $document_id})
                        OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
                        WITH d, collect(DISTINCT c) AS chunk_nodes
                        FOREACH (chunk IN chunk_nodes | DETACH DELETE chunk)
                        WITH d
                        OPTIONAL MATCH (d)-[:HAS_HEADING]->(h:Heading)
                        OPTIONAL MATCH (h)-[:HAS_SUBHEADING*0..]->(sub:Heading)
                        WITH d, collect(DISTINCT sub) AS heading_nodes
                        FOREACH (heading IN heading_nodes | DETACH DELETE heading)
                        WITH d
                        DETACH DELETE d
                        """,
                        document_id=document_id,
                    )
                return Ok(None)
            except Exception as exc:
                logger.error("neo4j_graph_store.delete_failed", document_id=document_id, error=str(exc))
                return Err(GraphStoreError("Neo4j graph store operation failed", cause=exc))

    @staticmethod
    def _merge_document(tx, document: dict, course_id: str | None, owner_id: str | None) -> None:
        document_id = document["id"]
        tx.run(
            """
            MERGE (d:Document {id: $document_id})
            ON CREATE SET d.created_at = datetime()
            SET d.name = $document_name,
                d.doc_type = $doc_type,
                d.updated_at = datetime()
            """,
            document_id=document["id"],
            document_name=document["name"],
            doc_type=document["doc_type"],
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
    def _build_heading_projection(document_id: str, chunks: list[GraphChunk]) -> dict[str, list[dict]]:
        chunk_rows = [
            {
                "chunk_id": chunk.chunk_id,
                "chunk_index": chunk.chunk_index,
                "page_number": chunk.page_number,
                "language": chunk.language,
            }
            for chunk in chunks
        ]

        next_edges = [
            {
                "previous_chunk_id": previous.chunk_id,
                "next_chunk_id": current.chunk_id,
            }
            for previous, current in zip(chunks, chunks[1:])
        ]

        headings_by_id: dict[str, dict] = {}
        root_heading_ids: set[str] = set()
        subheading_edges: set[tuple[str, str]] = set()
        heading_chunk_edges: set[tuple[str, str]] = set()

        for chunk in chunks:
            if not chunk.heading_path:
                continue

            parent_heading_id: str | None = None
            for level, title in enumerate(chunk.heading_path, start=1):
                heading_id = f"{document_id}:{level}:{' > '.join(chunk.heading_path[:level])}"
                headings_by_id.setdefault(
                    heading_id,
                    {
                        "id": heading_id,
                        "level": level,
                        "title": title,
                    },
                )

                if parent_heading_id is None:
                    root_heading_ids.add(heading_id)
                else:
                    subheading_edges.add((parent_heading_id, heading_id))

                parent_heading_id = heading_id

            if parent_heading_id is not None:
                heading_chunk_edges.add((parent_heading_id, chunk.chunk_id))

        return {
            "chunks": chunk_rows,
            "headings": list(headings_by_id.values()),
            "root_heading_edges": [{"heading_id": heading_id} for heading_id in sorted(root_heading_ids)],
            "subheading_edges": [
                {
                    "parent_heading_id": parent_heading_id,
                    "child_heading_id": child_heading_id,
                }
                for parent_heading_id, child_heading_id in sorted(subheading_edges)
            ],
            "heading_chunk_edges": [
                {
                    "heading_id": heading_id,
                    "chunk_id": chunk_id,
                }
                for heading_id, chunk_id in sorted(heading_chunk_edges)
            ],
            "next_edges": next_edges,
        }

    @staticmethod
    def _merge_heading_graph(
        tx,
        document: dict,
        course_id: str | None,
        owner_id: str | None,
        chunks: list[dict],
        headings: list[dict],
        root_heading_edges: list[dict],
        subheading_edges: list[dict],
        heading_chunk_edges: list[dict],
        next_edges: list[dict],
    ) -> None:
        document_id = document["id"]
        Neo4jGraphStore._merge_document(tx, document, course_id, owner_id)

        tx.run(
            """
            UNWIND $chunks AS chunk
            MERGE (c:Chunk {id: chunk.chunk_id})
            ON CREATE SET c.created_at = datetime()
            SET c.document_id = $document_id,
                c.chunk_index = chunk.chunk_index,
                c.page_number = chunk.page_number,
                c.language = chunk.language,
                c.updated_at = datetime()
            WITH c
            MATCH (d:Document {id: $document_id})
            MERGE (d)-[:HAS_CHUNK]->(c)
            """,
            document_id=document_id,
            chunks=chunks,
        )

        tx.run(
            """
            UNWIND $headings AS heading
            MERGE (h:Heading {id: heading.id})
            SET h.document_id = $document_id,
                h.level = heading.level,
                h.title = heading.title,
                h.updated_at = datetime()
            """,
            document_id=document_id,
            headings=headings,
        )

        tx.run(
            """
            UNWIND $root_heading_edges AS edge
            MATCH (d:Document {id: $document_id})
            MATCH (h:Heading {id: edge.heading_id})
            MERGE (d)-[:HAS_HEADING]->(h)
            """,
            document_id=document_id,
            root_heading_edges=root_heading_edges,
        )

        tx.run(
            """
            UNWIND $subheading_edges AS edge
            MATCH (parent:Heading {id: edge.parent_heading_id})
            MATCH (child:Heading {id: edge.child_heading_id})
            MERGE (parent)-[:HAS_SUBHEADING]->(child)
            """,
            subheading_edges=subheading_edges,
        )

        tx.run(
            """
            UNWIND $heading_chunk_edges AS edge
            MATCH (h:Heading {id: edge.heading_id})
            MATCH (c:Chunk {id: edge.chunk_id})
            MERGE (h)-[:HAS_CHUNK]->(c)
            """,
            heading_chunk_edges=heading_chunk_edges,
        )

        tx.run(
            """
            UNWIND $next_edges AS edge
            MATCH (a:Chunk {id: edge.previous_chunk_id})
            MATCH (b:Chunk {id: edge.next_chunk_id})
            MERGE (a)-[:NEXT]->(b)
            """,
            next_edges=next_edges,
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

    @staticmethod
    def _count_chunks(tx, chunk_ids: list[str]) -> int:
        record = tx.run(
            """
            MATCH (c:Chunk)
            WHERE c.id IN $chunk_ids
            RETURN count(c) AS count
            """,
            chunk_ids=chunk_ids,
        ).single()
        return int(record["count"]) if record is not None else 0

    @staticmethod
    def _merge_concepts(tx, concepts: list[dict]) -> None:
        tx.run(
            """
            UNWIND $concepts AS concept
            MERGE (c:Concept {id: concept.id})
            ON CREATE SET c.created_at = datetime()
            SET c.name = concept.name,
                c.canonical_name = concept.canonical_name,
                c.slug = concept.slug,
                c.category = concept.category,
                c.language = concept.language,
                c.domain = concept.domain,
                c.updated_at = datetime()
            """,
            concepts=concepts,
        )

    def upsert_curriculum_graph(
        self,
        course_id: str,
        course_code: str,
        course_title_vi: str,
        chapters: list[GraphChapter],
        los: list[GraphLO],
        assessments: list[GraphAssessment],
        lo_assessment_links: list[tuple[str, str]],
        chapter_lo_links: list[GraphChapterLOEdge] | None = None,
    ) -> Result[None, Exception]:
        with tracer.start_as_current_span("neo4j.upsert_curriculum_graph") as span:
            span.set_attribute("course_id", course_id)
            span.set_attribute("lo.count", len(los))
            try:
                with self._driver.session(database=self._config.database) as session:
                    session.execute_write(
                        self._merge_curriculum,
                        course_id,
                        course_code,
                        course_title_vi,
                        [{"id": c.chapter_id, "code": c.code, "title": c.title, "order_index": c.order_index} for c in chapters],
                        [
                            {
                                "id": lo.lo_id, "code": lo.code, "parent_code": lo.parent_code,
                                "statement_vi": lo.statement_vi, "statement_en": lo.statement_en,
                                "bloom_level": lo.bloom_level, "cdio_level": lo.cdio_level,
                            }
                            for lo in los
                        ],
                        [{"id": a.assessment_id, "code": a.code, "name_vi": a.name_vi, "category": a.category, "weight": a.weight} for a in assessments],
                        [{"lo_id": link[0], "assessment_id": link[1]} for link in lo_assessment_links],
                        [
                            {"chapter_id": e.chapter_id, "lo_id": e.lo_id,
                             "provenance": e.provenance}
                            for e in (chapter_lo_links or [])
                        ],
                    )
                return Ok(None)
            except Exception as exc:
                logger.error("neo4j_graph_store.curriculum_upsert_failed", course_id=course_id, error=str(exc))
                return Err(GraphStoreError("Neo4j graph store operation failed", cause=exc))

    def upsert_chunk_lo_mappings(
        self,
        mappings: list[GraphChunkLOEdge],
    ) -> Result[None, Exception]:
        with tracer.start_as_current_span("neo4j.upsert_chunk_lo_mappings") as span:
            span.set_attribute("mappings.count", len(mappings))
            try:
                with self._driver.session(database=self._config.database) as session:
                    session.execute_write(
                        self._merge_chunk_lo_supports,
                        [
                            {"chunk_id": m.chunk_id, "lo_id": m.lo_id, "confidence": m.confidence, "source": m.source}
                            for m in mappings
                        ],
                    )
                return Ok(None)
            except Exception as exc:
                return Err(GraphStoreError("Neo4j graph store operation failed", cause=exc))

    def find_chunks_for_lo(
        self, lo_id: str, limit: int = 20
    ) -> Result[list[str], Exception]:
        try:
            with self._driver.session(database=self._config.database) as session:
                result = session.execute_read(self._query_chunks_for_lo, lo_id, limit)
            return Ok(result)
        except Exception as exc:
            return Err(GraphStoreError("Neo4j graph store operation failed", cause=exc))

    def find_chunks_for_chapter(
        self, course_id: str, chapter_code: str, limit: int = 20
    ) -> Result[list[str], Exception]:
        try:
            with self._driver.session(database=self._config.database) as session:
                result = session.execute_read(self._query_chunks_for_chapter, course_id, chapter_code, limit)
            return Ok(result)
        except Exception as exc:
            return Err(GraphStoreError("Neo4j graph store operation failed", cause=exc))

    def find_los_for_assessment(
        self, course_id: str, assessment_code: str
    ) -> Result[list[str], Exception]:
        try:
            with self._driver.session(database=self._config.database) as session:
                result = session.execute_read(self._query_los_for_assessment, course_id, assessment_code)
            return Ok(result)
        except Exception as exc:
            return Err(GraphStoreError("Neo4j graph store operation failed", cause=exc))

    @staticmethod
    def _merge_curriculum(
        tx,
        course_id: str,
        course_code: str,
        course_title_vi: str,
        chapters: list[dict],
        los: list[dict],
        assessments: list[dict],
        lo_assessment_links: list[dict],
        chapter_lo_links: list[dict] | None = None,
    ) -> None:
        tx.run(
            """
            MERGE (c:Course {id: $course_id})
            ON CREATE SET c.created_at = datetime()
            SET c.code = $code, c.title_vi = $title_vi, c.updated_at = datetime()
            """,
            course_id=course_id, code=course_code, title_vi=course_title_vi,
        )

        tx.run(
            """
            UNWIND $chapters AS ch
            MERGE (c:Chapter {id: ch.id})
            ON CREATE SET c.created_at = datetime()
            SET c.code = ch.code, c.title = ch.title, c.order_index = ch.order_index, c.updated_at = datetime()
            WITH c, ch
            MATCH (course:Course {id: $course_id})
            MERGE (course)-[:HAS_CHAPTER]->(c)
            """,
            course_id=course_id, chapters=chapters,
        )

        tx.run(
            """
            UNWIND $los AS lo
            MERGE (l:LearningOutcome {id: lo.id})
            ON CREATE SET l.created_at = datetime()
            SET l.code = lo.code, l.parent_code = lo.parent_code,
                l.statement_vi = lo.statement_vi, l.statement_en = lo.statement_en,
                l.bloom_level = lo.bloom_level, l.cdio_level = lo.cdio_level,
                l.updated_at = datetime()
            WITH l, lo
            MATCH (course:Course {id: $course_id})
            MERGE (course)-[:HAS_LO]->(l)
            """,
            course_id=course_id, los=los,
        )

        tx.run(
            """
            UNWIND $los AS lo
            WITH lo WHERE lo.parent_code IS NOT NULL
            MATCH (course:Course {id: $course_id})-[:HAS_LO]->(parent:LearningOutcome {code: lo.parent_code})
            MATCH (child:LearningOutcome {id: lo.id})
            MERGE (parent)-[:PARENT_OF]->(child)
            """,
            course_id=course_id, los=los,
        )

        tx.run(
            """
            UNWIND $assessments AS a
            MERGE (asmt:Assessment {id: a.id})
            ON CREATE SET asmt.created_at = datetime()
            SET asmt.code = a.code, asmt.name_vi = a.name_vi,
                asmt.category = a.category, asmt.weight = a.weight, asmt.updated_at = datetime()
            WITH asmt, a
            MATCH (course:Course {id: $course_id})
            MERGE (course)-[:HAS_ASSESSMENT]->(asmt)
            """,
            course_id=course_id, assessments=assessments,
        )

        if lo_assessment_links:
            tx.run(
                """
                UNWIND $links AS link
                MATCH (l:LearningOutcome {id: link.lo_id})
                MATCH (a:Assessment {id: link.assessment_id})
                MERGE (l)-[:EVALUATED_BY]->(a)
                """,
                links=lo_assessment_links,
            )

        # Chương → LO, nhiều–nhiều, chiếu từ bảng mục 6. Trước đây đồ thị không
        # có cạnh này nên không trả lời được "chương 7 dạy chuẩn đầu ra nào".
        if chapter_lo_links:
            tx.run(
                """
                UNWIND $links AS link
                MATCH (ch:Chapter {id: link.chapter_id})
                MATCH (l:LearningOutcome {id: link.lo_id})
                MERGE (ch)-[r:COVERS]->(l)
                SET r.provenance = link.provenance, r.updated_at = datetime()
                """,
                links=chapter_lo_links,
            )

    @staticmethod
    def _merge_chunk_lo_supports(tx, mappings: list[dict]) -> None:
        tx.run(
            """
            UNWIND $mappings AS m
            MATCH (ch:Chunk {id: m.chunk_id})
            MATCH (lo:LearningOutcome {id: m.lo_id})
            MERGE (ch)-[r:SUPPORTS]->(lo)
            ON CREATE SET r.created_at = datetime()
            SET r.confidence = m.confidence, r.source = m.source, r.updated_at = datetime()
            """,
            mappings=mappings,
        )

    @staticmethod
    def _query_chunks_for_lo(tx, lo_id: str, limit: int) -> list[str]:
        result = tx.run(
            """
            MATCH (ch:Chunk)-[r:SUPPORTS]->(lo:LearningOutcome {id: $lo_id})
            RETURN ch.id AS chunk_id
            ORDER BY r.confidence DESC
            LIMIT $limit
            """,
            lo_id=lo_id, limit=limit,
        )
        return [record["chunk_id"] for record in result]

    @staticmethod
    def _query_chunks_for_chapter(tx, course_id: str, chapter_code: str, limit: int) -> list[str]:
        result = tx.run(
            """
            MATCH (course:Course {id: $course_id})-[:HAS_LO]->(lo:LearningOutcome)
            WHERE lo.code STARTS WITH ('L.O.' + $chapter_code)
            MATCH (ch:Chunk)-[:SUPPORTS]->(lo)
            RETURN DISTINCT ch.id AS chunk_id
            LIMIT $limit
            """,
            course_id=course_id, chapter_code=chapter_code, limit=limit,
        )
        return [record["chunk_id"] for record in result]

    @staticmethod
    def _query_los_for_assessment(tx, course_id: str, assessment_code: str) -> list[str]:
        result = tx.run(
            """
            MATCH (course:Course {id: $course_id})-[:HAS_ASSESSMENT]->(a:Assessment {code: $assessment_code})
            MATCH (lo:LearningOutcome)-[:EVALUATED_BY]->(a)
            RETURN lo.id AS lo_id
            """,
            course_id=course_id, assessment_code=assessment_code,
        )
        return [record["lo_id"] for record in result]

    @staticmethod
    def _merge_mentions(tx, mentions: list[dict]) -> None:
        tx.run(
            """
            UNWIND $mentions AS mention
            MATCH (chunk:Chunk {id: mention.chunk_id})
            MATCH (concept:Concept {id: mention.concept_id})
            MERGE (chunk)-[r:MENTIONS]->(concept)
            ON CREATE SET r.created_at = datetime()
            SET r.confidence = mention.confidence,
                r.source = mention.source,
                r.updated_at = datetime()
            """,
            mentions=mentions,
        )

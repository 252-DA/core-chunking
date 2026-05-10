"""
MapChunksToLosUseCase — map all chunks for a document to LOs of its course.

Triggered after ProcessDocument when document.course_id has a curriculum.
Runs best-effort, does not block the main pipeline.
"""
from dataclasses import dataclass

from src.domain.entities.curriculum import Curriculum
from src.domain.exceptions import ProcessingError
from src.domain.ports.chunk_lo_mapper import IChunkLOMapper
from src.domain.ports.graph_store import GraphChunkLOEdge, IGraphStore
from src.domain.ports.metadata_store import IMetadataStore, StoredChunkLOMapping
from src.shared.logger import get_logger
from src.shared.result import Err, Ok, Result

logger = get_logger(__name__)


@dataclass
class MapChunksToLosRequest:
    document_id: str
    course_id: str


@dataclass
class MapChunksToLosResponse:
    document_id: str
    course_id: str
    mapping_count: int


class MapChunksToLosUseCase:
    def __init__(
        self,
        metadata_store: IMetadataStore,
        graph_store: IGraphStore,
        lo_mapper: IChunkLOMapper,
    ) -> None:
        self._metadata_store = metadata_store
        self._graph_store = graph_store
        self._lo_mapper = lo_mapper

    def execute(
        self, request: MapChunksToLosRequest
    ) -> Result[MapChunksToLosResponse, Exception]:
        # 1. Fetch curriculum
        curriculum_result = self._metadata_store.get_curriculum(request.course_id)
        if curriculum_result.is_err():
            return Err(curriculum_result.error)

        raw = curriculum_result.unwrap()
        if raw is None:
            return Err(ProcessingError(f"No curriculum found for course {request.course_id}"))

        stored_course, stored_chapters, stored_los, stored_assessments = raw

        from src.domain.entities.curriculum import (
            Assessment,
            Chapter,
            Course,
            LearningOutcome,
        )

        curriculum = Curriculum(
            course=Course(
                course_id=stored_course.course_id,
                code=stored_course.code,
                title_vi=stored_course.title_vi,
                title_en=stored_course.title_en,
            ),
            chapters=tuple(
                Chapter(chapter_id=c.chapter_id, code=c.code, title=c.title, order_index=c.order_index)
                for c in stored_chapters
            ),
            learning_outcomes=tuple(
                LearningOutcome(
                    lo_id=lo.lo_id, code=lo.code, parent_code=lo.parent_code,
                    statement_vi=lo.statement_vi, statement_en=lo.statement_en,
                    bloom_level=lo.bloom_level, cdio_level=lo.cdio_level,
                )
                for lo in stored_los
            ),
            assessments=tuple(
                Assessment(
                    assessment_id=a.assessment_id, code=a.code, name_vi=a.name_vi,
                    name_en=a.name_en, category=a.category, weight=a.weight,
                )
                for a in stored_assessments
            ),
            lo_assessment_links=(),
            extraction_confidence=stored_course.extraction_confidence,
        )

        # 2. Fetch chunks for document
        chunks_result = self._metadata_store.list_chunks(request.document_id)
        if chunks_result.is_err():
            return Err(chunks_result.error)

        chunks = chunks_result.unwrap()
        if not chunks:
            return Ok(MapChunksToLosResponse(
                document_id=request.document_id,
                course_id=request.course_id,
                mapping_count=0,
            ))

        # 3. Map chunks to LOs
        map_result = self._lo_mapper.map(chunks, curriculum)
        if map_result.is_err():
            return Err(map_result.error)

        mappings = map_result.unwrap()
        if not mappings:
            return Ok(MapChunksToLosResponse(
                document_id=request.document_id,
                course_id=request.course_id,
                mapping_count=0,
            ))

        # 4. Persist to Postgres
        stored_mappings = [
            StoredChunkLOMapping(
                chunk_id=m.chunk_id,
                lo_id=m.lo_id,
                confidence=m.confidence,
                source=m.source,
            )
            for m in mappings
        ]
        persist_result = self._metadata_store.upsert_chunk_lo_mappings(stored_mappings)
        if persist_result.is_err():
            return Err(persist_result.error)

        # 5. Project to Neo4j (best-effort)
        neo4j_result = self._graph_store.upsert_chunk_lo_mappings([
            GraphChunkLOEdge(
                chunk_id=m.chunk_id,
                lo_id=m.lo_id,
                confidence=m.confidence,
                source=m.source,
            )
            for m in mappings
        ])
        if neo4j_result.is_err():
            logger.warning(
                "map_chunks_to_los.neo4j_failed",
                document_id=request.document_id,
                error=str(neo4j_result.error),
            )

        logger.info(
            "map_chunks_to_los.done",
            document_id=request.document_id,
            course_id=request.course_id,
            mappings=len(mappings),
        )
        return Ok(MapChunksToLosResponse(
            document_id=request.document_id,
            course_id=request.course_id,
            mapping_count=len(mappings),
        ))

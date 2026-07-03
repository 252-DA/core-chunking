"""
IngestCurriculumUseCase — parse DCMH → persist Postgres → project outbox → Neo4j.

Synchronous in MVP (called from gRPC handler directly).
"""
from dataclasses import dataclass
from pathlib import Path

from document_chunk.domain.entities.curriculum import Curriculum
from document_chunk.domain.exceptions import ProcessingError
from document_chunk.domain.ports.curriculum_extractor import ICurriculumExtractor
from document_chunk.domain.ports.graph_store import (
    GraphAssessment,
    GraphChapter,
    GraphChunkLOEdge,
    GraphLO,
    IGraphStore,
)
from document_chunk.domain.ports.metadata_store import (
    IMetadataStore,
    StoredAssessment,
    StoredChapter,
    StoredCourse,
    StoredLearningOutcome,
)
from document_chunk.domain.ports.parser import IParser
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)


@dataclass
class IngestCurriculumRequest:
    file_path: Path
    file_name: str
    course_id: str


@dataclass
class IngestCurriculumResponse:
    course_id: str
    course_code: str
    title_vi: str
    chapter_count: int
    lo_count: int
    assessment_count: int
    extraction_confidence: float
    warnings: list[str]


class IngestCurriculumUseCase:
    def __init__(
        self,
        parsers: list[IParser],
        curriculum_extractor: ICurriculumExtractor,
        metadata_store: IMetadataStore,
        graph_store: IGraphStore,
    ) -> None:
        self._parsers = parsers
        self._extractor = curriculum_extractor
        self._metadata_store = metadata_store
        self._graph_store = graph_store

    def execute(
        self, request: IngestCurriculumRequest
    ) -> Result[IngestCurriculumResponse, Exception]:
        # 1. Parse file
        parser = self._resolve_parser(request.file_name)
        if parser is None:
            return Err(ProcessingError(f"No parser for file: {request.file_name}"))

        parse_result = parser.parse(request.file_path)
        if parse_result.is_err():
            return Err(parse_result.error)

        parsed_doc = parse_result.unwrap()

        # 2. Extract curriculum
        extract_result = self._extractor.extract(parsed_doc, course_id_hint=request.course_id)
        if extract_result.is_err():
            return Err(extract_result.error)

        curriculum: Curriculum = extract_result.unwrap()

        # 3. Persist to Postgres (source of truth)
        persist_result = self._persist_to_postgres(curriculum)
        if persist_result.is_err():
            return Err(persist_result.error)

        # 4. Project to Neo4j (best-effort — don't fail if Neo4j is down)
        neo4j_result = self._project_to_neo4j(curriculum)
        neo4j_warnings: list[str] = []
        if neo4j_result.is_err():
            logger.warning(
                "ingest_curriculum.neo4j_failed",
                course_id=curriculum.course.course_id,
                error=str(neo4j_result.error),
            )
            neo4j_warnings = [f"Neo4j projection failed: {neo4j_result.error}"]

        return Ok(IngestCurriculumResponse(
            course_id=curriculum.course.course_id,
            course_code=curriculum.course.code,
            title_vi=curriculum.course.title_vi,
            chapter_count=len(curriculum.chapters),
            lo_count=len(curriculum.learning_outcomes),
            assessment_count=len(curriculum.assessments),
            extraction_confidence=curriculum.extraction_confidence,
            warnings=neo4j_warnings,
        ))

    def _resolve_parser(self, file_name: str) -> IParser | None:
        suffix = Path(file_name).suffix.lower().lstrip(".")
        for parser in self._parsers:
            if suffix in (s.lstrip(".").lower() for s in getattr(parser, "supported_extensions", [])):
                return parser
            if hasattr(parser, "supports") and parser.supports(suffix):
                return parser
        # fallback: try each parser's doc_type
        for parser in self._parsers:
            doc_type = getattr(parser, "_doc_type", None) or getattr(parser, "doc_type", None)
            if doc_type and doc_type.value == suffix:
                return parser
        if self._parsers:
            return self._parsers[0]
        return None

    def _persist_to_postgres(self, curriculum: Curriculum) -> Result[None, Exception]:
        course = curriculum.course
        stored_course = StoredCourse(
            course_id=course.course_id,
            code=course.code,
            title_vi=course.title_vi,
            title_en=course.title_en,
            credits=course.credits,
            semester=course.semester,
            source_document_id=curriculum.source_document_id,
            extraction_confidence=curriculum.extraction_confidence,
        )

        chapters = [
            StoredChapter(
                chapter_id=ch.chapter_id,
                course_id=course.course_id,
                code=ch.code,
                title=ch.title,
                order_index=ch.order_index,
            )
            for ch in curriculum.chapters
        ]

        los = [
            StoredLearningOutcome(
                lo_id=lo.lo_id,
                course_id=course.course_id,
                code=lo.code,
                parent_code=lo.parent_code,
                statement_vi=lo.statement_vi,
                statement_en=lo.statement_en,
                bloom_level=lo.bloom_level,
                cdio_level=lo.cdio_level,
            )
            for lo in curriculum.learning_outcomes
        ]

        assessments = [
            StoredAssessment(
                assessment_id=a.assessment_id,
                course_id=course.course_id,
                code=a.code,
                name_vi=a.name_vi,
                name_en=a.name_en,
                category=a.category,
                weight=a.weight,
            )
            for a in curriculum.assessments
        ]

        return self._metadata_store.upsert_curriculum(
            course=stored_course,
            chapters=chapters,
            learning_outcomes=los,
            assessments=assessments,
            lo_assessment_links=list(curriculum.lo_assessment_links),
        )

    def _project_to_neo4j(self, curriculum: Curriculum) -> Result[None, Exception]:
        course = curriculum.course
        return self._graph_store.upsert_curriculum_graph(
            course_id=course.course_id,
            course_code=course.code,
            course_title_vi=course.title_vi,
            chapters=[
                GraphChapter(
                    chapter_id=ch.chapter_id,
                    code=ch.code,
                    title=ch.title,
                    order_index=ch.order_index,
                )
                for ch in curriculum.chapters
            ],
            los=[
                GraphLO(
                    lo_id=lo.lo_id,
                    code=lo.code,
                    parent_code=lo.parent_code,
                    statement_vi=lo.statement_vi,
                    statement_en=lo.statement_en,
                    bloom_level=lo.bloom_level,
                    cdio_level=lo.cdio_level,
                )
                for lo in curriculum.learning_outcomes
            ],
            assessments=[
                GraphAssessment(
                    assessment_id=a.assessment_id,
                    code=a.code,
                    name_vi=a.name_vi,
                    category=a.category,
                    weight=a.weight,
                )
                for a in curriculum.assessments
            ],
            lo_assessment_links=list(curriculum.lo_assessment_links),
        )

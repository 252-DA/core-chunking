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
    GraphChapterLOEdge,
    GraphChunkLOEdge,
    GraphLO,
    IGraphStore,
)
from document_chunk.domain.ports.metadata_store import (
    IMetadataStore,
    StoredAssessment,
    StoredChapter,
    StoredChapterLOLink,
    StoredCourse,
    StoredCourseGoal,
    StoredCourseSession,
    StoredExtractionIssue,
    StoredLearningOutcome,
    StoredLOAssessmentLink,
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
class ExtractionIssueView:
    code: str
    severity: str
    message: str
    source: str | None = None


@dataclass
class IngestCurriculumResponse:
    course_id: str
    course_code: str
    title_vi: str
    chapter_count: int
    lo_count: int
    assessment_count: int
    session_count: int
    chapter_lo_link_count: int
    # Thay cho một điểm confidence trung bình: danh sách cụ thể các chỗ cần soát.
    issues: list[ExtractionIssueView]
    warnings: list[str]

    @property
    def needs_review(self) -> bool:
        return bool(self.issues)


class IngestCurriculumUseCase:
    def __init__(
        self,
        parsers: list[IParser],
        curriculum_extractor: ICurriculumExtractor,
        metadata_store: IMetadataStore,
        graph_store: IGraphStore,
        *,
        persist_on_blocking_issues: bool = False,
    ) -> None:
        self._parsers = parsers
        self._extractor = curriculum_extractor
        self._metadata_store = metadata_store
        self._graph_store = graph_store
        # Mặc định không ghi khi còn lỗi chặn: đề cương sai cấu trúc mà vào
        # Postgres rồi thì mọi thứ dựng trên nó (bài học, quiz) đều lệch theo.
        self._persist_on_blocking_issues = persist_on_blocking_issues

    def extract(self, request: IngestCurriculumRequest) -> Result[Curriculum, Exception]:
        """Parse + trích xuất, không ghi gì — dùng cho bản xem trước trước khi áp dụng."""
        parser = self._resolve_parser(request.file_name)
        if parser is None:
            return Err(ProcessingError(f"No parser for file: {request.file_name}"))

        parse_result = parser.parse(request.file_path)
        if parse_result.is_err():
            return Err(parse_result.error)

        return self._extractor.extract(parse_result.unwrap(), course_id_hint=request.course_id)

    def execute(
        self, request: IngestCurriculumRequest
    ) -> Result[IngestCurriculumResponse, Exception]:
        # 1–2. Parse file + extract curriculum
        extract_result = self.extract(request)
        if extract_result.is_err():
            return Err(extract_result.error)

        curriculum: Curriculum = extract_result.unwrap()

        # 3. Cổng duyệt: còn lỗi chặn thì dừng, không ghi vào nguồn sự thật.
        blocking = curriculum.blocking_issues
        if blocking and not self._persist_on_blocking_issues:
            logger.warning(
                "ingest_curriculum.blocked",
                course_id=curriculum.course.course_id,
                errors=[i.code for i in blocking],
            )
            return Err(ProcessingError(
                "Đề cương chưa đạt kiểm tra cấu trúc, không ghi vào Postgres: "
                + "; ".join(f"{i.code}: {i.message}" for i in blocking[:5])
            ))

        # 4. Persist to Postgres (source of truth)
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
            session_count=len(curriculum.sessions),
            chapter_lo_link_count=len(curriculum.chapter_lo_links),
            issues=[
                ExtractionIssueView(
                    code=i.code,
                    severity=i.severity,
                    message=i.message,
                    source=str(i.source) if i.source else None,
                )
                for i in curriculum.issues
            ],
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
        chapter_id_by_code = {ch.code: ch.chapter_id for ch in curriculum.chapters}
        lo_id_by_code = {lo.code: lo.lo_id for lo in curriculum.learning_outcomes}
        ao_id_by_code = {a.code: a.assessment_id for a in curriculum.assessments}

        stored_course = StoredCourse(
            course_id=course.course_id,
            code=course.code,
            title_vi=course.title_vi,
            title_en=course.title_en,
            credits=course.credits,
            semester=course.semester,
            syllabus_version=course.syllabus_version,
            lo_year=course.lo_year,
            source_document_id=curriculum.source_document_id,
        )

        chapters = [
            StoredChapter(
                chapter_id=ch.chapter_id,
                course_id=course.course_id,
                code=ch.code,
                title=ch.title,
                order_index=ch.order_index,
                title_en=ch.title_en,
                source_section=ch.source.section if ch.source else None,
                source_page=ch.source.page if ch.source else None,
                topics="; ".join(ch.topics) or None,
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
                bloom_provenance=lo.bloom_provenance,
                cdio_provenance=lo.cdio_provenance,
                source_section=lo.source.section if lo.source else None,
                source_page=lo.source.page if lo.source else None,
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
                parent_code=a.parent_code,
                activity_type=a.activity_type,
                weight_provenance=a.weight_provenance,
                source_section=a.source.section if a.source else None,
                source_page=a.source.page if a.source else None,
            )
            for a in curriculum.assessments
        ]

        chapter_lo_links = [
            StoredChapterLOLink(
                chapter_id=chapter_id_by_code[link.chapter_code],
                lo_id=lo_id_by_code[link.lo_code],
                provenance=link.provenance,
                source_section=link.source.section if link.source else None,
                source_page=link.source.page if link.source else None,
            )
            for link in curriculum.chapter_lo_links
            if link.chapter_code in chapter_id_by_code and link.lo_code in lo_id_by_code
        ]

        lo_assessment_links = [
            StoredLOAssessmentLink(
                lo_id=lo_id_by_code[link.lo_code],
                assessment_id=ao_id_by_code[link.assessment_code],
                session_order=link.row_order_index or 0,
                scope="session" if link.row_order_index is not None else "course",
                chapter_id=chapter_id_by_code.get(link.chapter_code) if link.chapter_code else None,
                provenance=link.provenance,
                source_section=link.source.section if link.source else None,
                source_page=link.source.page if link.source else None,
            )
            for link in curriculum.lo_assessment_links
            if link.lo_code in lo_id_by_code and link.assessment_code in ao_id_by_code
        ]

        goals = [
            StoredCourseGoal(
                course_id=course.course_id,
                code=g.code,
                statement_vi=g.statement_vi,
                statement_en=g.statement_en,
                source_section=g.source.section if g.source else None,
                source_page=g.source.page if g.source else None,
            )
            for g in curriculum.goals
        ]

        sessions = [
            StoredCourseSession(
                course_id=course.course_id,
                order_index=row.order_index,
                title_vi=row.title_vi,
                session_no=row.session_no,
                chapter_id=chapter_id_by_code.get(row.chapter_code) if row.chapter_code else None,
                title_en=row.title_en,
                source_section=row.source.section if row.source else None,
                source_page=row.source.page if row.source else None,
            )
            for row in curriculum.sessions
        ]

        issues = [
            StoredExtractionIssue(
                course_id=course.course_id,
                code=i.code,
                severity=i.severity,
                message=i.message,
                document_id=curriculum.source_document_id,
                source_section=i.source.section if i.source else None,
                source_page=i.source.page if i.source else None,
                source_locator=i.source.locator if i.source else None,
            )
            for i in curriculum.issues
        ]

        return self._metadata_store.upsert_curriculum(
            course=stored_course,
            chapters=chapters,
            learning_outcomes=los,
            assessments=assessments,
            lo_assessment_links=lo_assessment_links,
            chapter_lo_links=chapter_lo_links,
            goals=goals,
            sessions=sessions,
            issues=issues,
        )

    def _project_to_neo4j(self, curriculum: Curriculum) -> Result[None, Exception]:
        course = curriculum.course
        lo_id_by_code = {lo.code: lo.lo_id for lo in curriculum.learning_outcomes}
        ao_id_by_code = {a.code: a.assessment_id for a in curriculum.assessments}
        chapter_id_by_code = {ch.code: ch.chapter_id for ch in curriculum.chapters}
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
            lo_assessment_links=[
                (lo_id_by_code[link.lo_code], ao_id_by_code[link.assessment_code])
                for link in curriculum.lo_assessment_links
                if link.lo_code in lo_id_by_code and link.assessment_code in ao_id_by_code
            ],
            chapter_lo_links=[
                GraphChapterLOEdge(
                    chapter_id=chapter_id_by_code[link.chapter_code],
                    lo_id=lo_id_by_code[link.lo_code],
                    provenance=link.provenance,
                )
                for link in curriculum.chapter_lo_links
                if link.chapter_code in chapter_id_by_code and link.lo_code in lo_id_by_code
            ],
        )

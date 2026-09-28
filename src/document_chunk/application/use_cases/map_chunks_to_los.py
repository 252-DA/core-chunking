"""
MapChunksToLosUseCase — map all chunks for a document to LOs of its course.

Triggered after ProcessDocument when document.course_id has a curriculum.
Runs best-effort, does not block the main pipeline.
"""
from dataclasses import dataclass

from document_chunk.domain.entities.curriculum import Curriculum
from document_chunk.domain.exceptions import ProcessingError
from document_chunk.domain.ports.chapter_matcher import ChapterProfile, IChapterMatcher
from document_chunk.domain.ports.chunk_lo_mapper import IChunkLOMapper, MappingHints
from document_chunk.domain.ports.graph_store import GraphChunkLOEdge, IGraphStore
from document_chunk.domain.ports.metadata_store import (
    IMetadataStore,
    StoredChunkLOMapping,
    StoredChunkMetadata,
)
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)

# Văn bản đại diện cho tài liệu/một phần tài liệu khi khớp với chương: các
# heading đầu (tiêu đề slide, mục sách) cộng vài đoạn nội dung đầu tiên.
_MAX_HEADINGS = 40
_MAX_CONTENT_CHUNKS = 3
_MAX_TEXT_CHARS = 2500
# Chunk không có heading được gom theo cửa sổ cố định để vẫn khớp được từng phần.
_SECTION_WINDOW = 8


def _representative_text(chunks: list[StoredChunkMetadata]) -> str:
    headings: list[str] = []
    for chunk in chunks:
        for heading in chunk.heading_path:
            heading = heading.strip()
            if heading and heading not in headings:
                headings.append(heading)
        if len(headings) >= _MAX_HEADINGS:
            break
    contents = [
        (chunk.content_text or "").strip()
        for chunk in chunks[:_MAX_CONTENT_CHUNKS]
        if (chunk.content_text or "").strip()
    ]
    return "\n".join(headings[:_MAX_HEADINGS] + contents)[:_MAX_TEXT_CHARS]


def _sections(chunks: list[StoredChunkMetadata]) -> list[list[StoredChunkMetadata]]:
    """Chia tài liệu tham khảo thành từng phần để khớp riêng với chương.

    Nhóm theo heading cấp cao nhất; nếu cả tài liệu chung một heading gốc (tên
    sách) thì xuống một cấp. Chunk không có heading gom theo cửa sổ cố định.
    """
    level = 0
    top = {c.heading_path[0] for c in chunks if c.heading_path}
    if len(top) <= 1 and any(len(c.heading_path) > 1 for c in chunks):
        level = 1

    groups: dict[str, list[StoredChunkMetadata]] = {}
    for chunk in chunks:
        if len(chunk.heading_path) > level:
            key = f"h:{chunk.heading_path[level]}"
        else:
            key = f"w:{chunk.chunk_index // _SECTION_WINDOW}"
        groups.setdefault(key, []).append(chunk)
    return list(groups.values())


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
        chapter_matcher: IChapterMatcher | None = None,
    ) -> None:
        self._metadata_store = metadata_store
        self._graph_store = graph_store
        self._lo_mapper = lo_mapper
        # Không có matcher (script backfill, test) thì chỉ dùng chương đã biết.
        self._chapter_matcher = chapter_matcher

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

        from document_chunk.domain.entities.curriculum import (
            Assessment,
            Chapter,
            ChapterLOLink,
            Course,
            LearningOutcome,
        )

        # Quan hệ chương ↔ LO là căn cứ duy nhất để gắn chunk; thiếu nó thì
        # mapper không có gì để dựa vào, nên lấy tường minh chứ không suy từ mã.
        links_result = self._metadata_store.list_chapter_lo_links(request.course_id)
        if links_result.is_err():
            return Err(links_result.error)
        stored_links = links_result.unwrap()

        chapter_code_by_id = {c.chapter_id: c.code for c in stored_chapters}
        lo_code_by_id = {lo.lo_id: lo.code for lo in stored_los}

        curriculum = Curriculum(
            course=Course(
                course_id=stored_course.course_id,
                code=stored_course.code,
                title_vi=stored_course.title_vi,
                title_en=stored_course.title_en,
            ),
            chapters=tuple(
                Chapter(
                    chapter_id=c.chapter_id,
                    code=c.code,
                    title=c.title,
                    order_index=c.order_index,
                    title_en=c.title_en,
                    topics=tuple(t for t in (c.topics or "").split("; ") if t),
                )
                for c in stored_chapters
            ),
            learning_outcomes=tuple(
                LearningOutcome(
                    lo_id=lo.lo_id, code=lo.code, parent_code=lo.parent_code,
                    statement_vi=lo.statement_vi, statement_en=lo.statement_en,
                    bloom_level=lo.bloom_level, cdio_level=lo.cdio_level,
                    bloom_provenance=lo.bloom_provenance,
                    cdio_provenance=lo.cdio_provenance,
                )
                for lo in stored_los
            ),
            assessments=tuple(
                Assessment(
                    assessment_id=a.assessment_id, code=a.code, name_vi=a.name_vi,
                    name_en=a.name_en, category=a.category, weight=a.weight,
                    parent_code=a.parent_code, activity_type=a.activity_type,
                )
                for a in stored_assessments
            ),
            lo_assessment_links=(),
            chapter_lo_links=tuple(
                ChapterLOLink(
                    chapter_code=chapter_code_by_id[link.chapter_id],
                    lo_code=lo_code_by_id[link.lo_id],
                    provenance=link.provenance,
                )
                for link in stored_links
                if link.chapter_id in chapter_code_by_id and link.lo_id in lo_code_by_id
            ),
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

        # 3. Map chunks to LOs, theo vị trí của tài liệu trong học phần
        hints = self._hints(request.document_id, chunks, curriculum)
        map_result = self._lo_mapper.map(chunks, curriculum, hints)
        if map_result.is_err():
            return Err(map_result.error)

        mappings = map_result.unwrap()

        # Chương của tài liệu có thể vừa đổi (giảng viên chọn lại, đồng bộ đề
        # cương mới): xoá cạnh suy luận cũ để không còn LO của chương trước.
        # Cạnh giảng viên đã xác nhận được giữ.
        cleared = self._metadata_store.delete_inferred_chunk_lo_mappings(request.document_id)
        if cleared.is_err():
            return Err(cleared.error)

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
        # list_chunks trả chunk_id kiểu uuid.UUID; driver Neo4j chỉ nhận str.
        neo4j_result = self._graph_store.upsert_chunk_lo_mappings([
            GraphChunkLOEdge(
                chunk_id=str(m.chunk_id),
                lo_id=str(m.lo_id),
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

    # ------------------------------------------------------------------
    # Vị trí của tài liệu: vai trò + chương
    # ------------------------------------------------------------------

    def _hints(
        self, document_id: str, chunks: list[StoredChunkMetadata], curriculum: Curriculum
    ) -> MappingHints:
        placement_result = self._metadata_store.get_document_placement(document_id)
        placement = placement_result.unwrap() if placement_result.is_ok() else None
        if placement is None:
            return MappingHints()

        # Chương không gắn LO nào ("Ôn tập") không đáng để khớp: gán vào đó
        # không sinh cạnh LO, chỉ hút nhầm các tài liệu chung chung.
        profiles = [
            ChapterProfile(
                code=ch.code,
                text=". ".join(filter(None, [ch.title, ch.title_en, "; ".join(ch.topics)])),
            )
            for ch in curriculum.chapters
            if curriculum.los_for_chapter(ch.code)
        ]

        if placement.role == "reference":
            return MappingHints(
                role="reference",
                chunk_chapters=self._match_sections(document_id, chunks, profiles),
            )

        chapter = placement.chapter_code
        if chapter is None:
            chapter = self._match_document(document_id, chunks, profiles)
        return MappingHints(role=placement.role, document_chapter=chapter)

    def _match_document(
        self, document_id: str, chunks: list[StoredChunkMetadata], profiles: list[ChapterProfile]
    ) -> str | None:
        if self._chapter_matcher is None or not profiles:
            return None
        text = _representative_text(chunks)
        if not text:
            return None
        result = self._chapter_matcher.match([text], profiles)
        if result.is_err():
            logger.warning("map_chunks_to_los.chapter_match_failed", document_id=document_id,
                           error=str(result.error))
            return None

        match = result.unwrap()[0]
        logger.info(
            "map_chunks_to_los.document_chapter_match",
            document_id=document_id,
            best=match.best_code,
            runner_up=match.runner_up_code,
            score=match.score,
            margin=match.margin,
            accepted=match.chapter_code is not None,
        )
        if match.chapter_code is None:
            return None
        reason = (
            f"Khớp nội dung với chương {match.chapter_code} (điểm {match.score:.2f}, "
            f"hơn chương {match.runner_up_code} {match.margin:.2f})"
        )
        self._metadata_store.set_document_chapter(
            document_id, match.chapter_code, "content", match.score, reason
        )
        return match.chapter_code

    def _match_sections(
        self, document_id: str, chunks: list[StoredChunkMetadata], profiles: list[ChapterProfile]
    ) -> dict[str, str]:
        if self._chapter_matcher is None or not profiles or not chunks:
            return {}
        sections = [s for s in _sections(chunks) if _representative_text(s)]
        result = self._chapter_matcher.match(
            [_representative_text(s) for s in sections], profiles, strict=True
        )
        if result.is_err():
            logger.warning("map_chunks_to_los.section_match_failed", document_id=document_id,
                           error=str(result.error))
            return {}

        chunk_chapters: dict[str, str] = {}
        for section, match in zip(sections, result.unwrap()):
            if match.chapter_code is None:
                continue
            for chunk in section:
                chunk_chapters[chunk.chunk_id] = match.chapter_code
        logger.info(
            "map_chunks_to_los.section_chapter_match",
            document_id=document_id,
            sections=len(sections),
            matched_sections=sum(1 for m in result.unwrap() if m.chapter_code),
            matched_chunks=len(chunk_chapters),
        )
        return chunk_chapters

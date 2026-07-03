"""
HeuristicLoMapper — heading-match-only chunk→LO mapper (MVP).

MVP: signal 1 (heading) only.
Phase 2: + signal 2 (embedding cosine) + signal 3 (LLM tiebreaker).
"""
import re

from document_chunk.domain.entities.curriculum import Curriculum, LearningOutcome
from document_chunk.domain.exceptions import ProcessingError
from document_chunk.domain.ports.chunk_lo_mapper import ChunkLOMapping, IChunkLOMapper
from document_chunk.domain.ports.metadata_store import StoredChunkMetadata
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)

_RE_CHAPTER_NUM = re.compile(r"[Cc]hương\s+(\d+)|^(\d+)\s*[.:\-]", re.MULTILINE)


def _extract_chapter_num(heading_path: tuple[str, ...]) -> str | None:
    for h in heading_path:
        m = _RE_CHAPTER_NUM.search(h)
        if m:
            return m.group(1) or m.group(2)
    return None


def _lo_chapter_num(lo: LearningOutcome) -> str | None:
    # L.O.4.2 → chapter "4"; L.O.4 → chapter "4"
    m = re.match(r"L\.O\.(\d+)", lo.code)
    return m.group(1) if m else None


def _heading_text_matches_lo(heading_path: tuple[str, ...], lo: LearningOutcome) -> bool:
    heading_lower = " ".join(heading_path).lower()
    lo_vi_words = lo.statement_vi.lower().split()[:4]
    matches = sum(1 for w in lo_vi_words if len(w) > 3 and w in heading_lower)
    return matches >= 2


class HeuristicLoMapper(IChunkLOMapper):
    """MVP: heading-chapter alignment only."""

    def map(
        self,
        chunks: list[StoredChunkMetadata],
        curriculum: Curriculum,
    ) -> Result[list[ChunkLOMapping], Exception]:
        try:
            mappings: dict[tuple[str, str], ChunkLOMapping] = {}

            for chunk in chunks:
                if not chunk.heading_path:
                    continue

                chapter_num = _extract_chapter_num(chunk.heading_path)
                if chapter_num is None:
                    continue

                for lo in curriculum.learning_outcomes:
                    lo_ch = _lo_chapter_num(lo)
                    if lo_ch != chapter_num:
                        continue

                    # Heading → LO chapter match → confidence 0.7
                    conf = 0.7
                    # Fuzzy heading text match → bump to 0.9
                    if _heading_text_matches_lo(chunk.heading_path, lo):
                        conf = 0.9

                    key = (chunk.chunk_id, lo.lo_id)
                    existing = mappings.get(key)
                    if existing is None or existing.confidence < conf:
                        mappings[key] = ChunkLOMapping(
                            chunk_id=chunk.chunk_id,
                            lo_id=lo.lo_id,
                            confidence=conf,
                            source="heading",
                        )

            result = list(mappings.values())
            logger.info(
                "heuristic_lo_mapper.done",
                course_id=curriculum.course.course_id,
                chunks=len(chunks),
                mappings=len(result),
            )
            return Ok(result)
        except Exception as exc:
            return Err(ProcessingError("HeuristicLoMapper failed", cause=exc))

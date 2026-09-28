"""
HeuristicLoMapper — gắn chunk tài liệu với LO qua **chương**, không qua mã LO.

Đường đi: chunk → nhận ra chương → tra đề cương xem chương đó phục vụ LO nào.
Bước cuối lấy từ ``curriculum.chapter_lo_links`` (bảng mục 6), không suy từ số
đầu của mã LO. ``L.O.2.2`` được dạy ở chương 4, 5, 7 và 12; đọc "2" trong mã rồi
coi đó là chương 2 vừa sai chiều vừa sai số.

Cạnh sinh ra ở đây là **ứng viên**: chương có liên quan tới LO không chứng minh
mọi chunk trong chương đều trực tiếp phục vụ LO đó. Vì vậy provenance luôn là
``inferred`` và confidence giữ ở mức vừa phải cho tới khi có tín hiệu nội dung
(pha sau) hoặc giảng viên xác nhận.
"""
import re
import unicodedata

from document_chunk.domain.entities.curriculum import Chapter, Curriculum
from document_chunk.domain.exceptions import ProcessingError
from document_chunk.domain.ports.chunk_lo_mapper import ChunkLOMapping, IChunkLOMapper, MappingHints
from document_chunk.domain.ports.metadata_store import StoredChunkMetadata
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)

# Chỉ nhận số chương khi có từ khoá đi kèm. Một số trần như "3.6" trong
# "3.6 Bước 4: ..." là mục con của giáo trình, không phải chương.
#
# Slide của giảng viên hiếm khi ghi đúng chữ "Chương": thực tế gặp "Bài 4",
# "Chapter 4", "Lecture 4", "Tuần 4", "Buổi 4". Nhận cả các biến thể này.
_CHAPTER_WORDS = ("chương", "chuong", "chapter", "bài", "bai", "lecture", "tuần", "tuan", "buổi", "buoi", "unit")
_RE_CHAPTER_WORD = re.compile(
    r"(?:" + "|".join(_CHAPTER_WORDS) + r")\s*[.:\-]?\s*0*(\d+)",
    re.IGNORECASE,
)

# Ngưỡng: chỉ-chương → ứng viên yếu; chương + tiêu đề khớp nội dung LO → mạnh hơn.
_CONF_CHAPTER_ONLY = 0.5
_CONF_CHAPTER_AND_HEADING = 0.7

_STOPWORDS = {
    "được", "các", "một", "của", "và", "cho", "trong", "với", "những", "này",
    "có", "để", "là", "về", "theo", "từ", "khi", "như", "hay", "phần", "mềm",
}


def _fold(text: str) -> str:
    """Bỏ dấu và hạ chữ thường để so khớp tiêu đề không phụ thuộc cách gõ dấu."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return stripped.replace("đ", "d")


def _content_words(text: str) -> set[str]:
    return {
        w for w in re.findall(r"\w+", text.lower())
        if len(w) > 3 and w not in _STOPWORDS
    }


class HeuristicLoMapper(IChunkLOMapper):
    """Chunk → chương → LO, dựa trên quan hệ chương–LO mà đề cương ghi."""

    def map(
        self,
        chunks: list[StoredChunkMetadata],
        curriculum: Curriculum,
        hints: MappingHints | None = None,
    ) -> Result[list[ChunkLOMapping], Exception]:
        hints = hints or MappingHints()
        try:
            if not curriculum.chapter_lo_links:
                logger.warning(
                    "heuristic_lo_mapper.no_chapter_lo_links",
                    course_id=curriculum.course.course_id,
                    reason="đề cương chưa có quan hệ chương–LO; không có căn cứ để gắn",
                )
                return Ok([])

            lo_id_by_code = {lo.code: lo.lo_id for lo in curriculum.learning_outcomes}
            mappings: dict[tuple[str, str], ChunkLOMapping] = {}
            matched_chunks = 0

            for chunk in chunks:
                chapter, origin = self._chapter_for(chunk, curriculum, hints)
                if chapter is None:
                    continue
                matched_chunks += 1

                heading_words = _content_words(" ".join(chunk.heading_path))
                for lo_code in curriculum.los_for_chapter(chapter.code):
                    lo_id = lo_id_by_code.get(lo_code)
                    if lo_id is None:
                        # Đề cương tham chiếu một mã LO mà mục 4.2 không định
                        # nghĩa — đã được extractor báo là lỗi; không đoán thêm.
                        continue

                    lo = curriculum.lo_by_code[lo_code]
                    confidence = _CONF_CHAPTER_ONLY
                    source = origin
                    if self._heading_matches_lo(heading_words, lo.statement_vi):
                        confidence = _CONF_CHAPTER_AND_HEADING
                        source = f"{origin}+heading"

                    key = (chunk.chunk_id, lo_id)
                    existing = mappings.get(key)
                    if existing is None or existing.confidence < confidence:
                        mappings[key] = ChunkLOMapping(
                            chunk_id=chunk.chunk_id,
                            lo_id=lo_id,
                            confidence=confidence,
                            source=source,
                        )

            result = list(mappings.values())
            logger.info(
                "heuristic_lo_mapper.done",
                course_id=curriculum.course.course_id,
                chunks=len(chunks),
                chunks_with_chapter=matched_chunks,
                mappings=len(result),
            )
            return Ok(result)
        except Exception as exc:
            return Err(ProcessingError("HeuristicLoMapper failed", cause=exc))

    # ------------------------------------------------------------------

    def _chapter_for(
        self, chunk: StoredChunkMetadata, curriculum: Curriculum, hints: MappingHints
    ) -> tuple[Chapter | None, str]:
        """Chương của chunk và nguồn của quyết định ("section" | "document" | "chapter")."""
        by_code = curriculum.chapter_by_code
        if (code := hints.chunk_chapters.get(chunk.chunk_id)) is not None:
            return by_code.get(code), "section"
        if hints.role == "reference":
            # Sách tham khảo đánh số chương theo sách chứ không theo đề cương:
            # "Chapter 3" của Mining of Massive Datasets là chương 2 của học
            # phần. Đoán theo số sẽ gắn sai, nên chỉ tin phần đã khớp nội dung.
            return None, ""
        if hints.document_chapter is not None:
            return by_code.get(hints.document_chapter), "document"
        if not chunk.heading_path:
            return None, ""
        return self._detect_chapter(chunk.heading_path, curriculum), "chapter"

    def _detect_chapter(
        self, heading_path: tuple[str, ...], curriculum: Curriculum
    ) -> Chapter | None:
        """Nhận chương từ breadcrumb: ưu tiên "Chương n", sau đó khớp tiêu đề."""
        joined = " ".join(heading_path)
        by_code = curriculum.chapter_by_code

        for m in _RE_CHAPTER_WORD.finditer(joined):
            chapter = by_code.get(m.group(1))
            if chapter is not None:
                return chapter

        folded_path = _fold(joined)
        best: tuple[int, Chapter] | None = None
        for chapter in curriculum.chapters:
            folded_title = _fold(chapter.title)
            if len(folded_title) >= 8 and folded_title in folded_path:
                # Tiêu đề dài hơn thắng, tránh "Quản lý rủi ro" nuốt "Rủi ro".
                if best is None or len(folded_title) > best[0]:
                    best = (len(folded_title), chapter)
        return best[1] if best else None

    def _heading_matches_lo(self, heading_words: set[str], statement_vi: str) -> bool:
        if not heading_words:
            return False
        return len(heading_words & _content_words(statement_vi)) >= 2

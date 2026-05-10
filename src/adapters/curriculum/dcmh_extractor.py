"""
DcmhExtractor — structural-only DCMH parser (MVP).

Parses Đề cương môn học theo định dạng CDIO của BKHCM bằng regex.
LLM fallback dành cho Phase 2.
"""
import re
import uuid
from dataclasses import dataclass, field

from src.domain.entities.curriculum import (
    Assessment,
    Chapter,
    Course,
    Curriculum,
    LearningOutcome,
)
from src.domain.entities.document import ParsedDocument
from src.domain.exceptions import ProcessingError
from src.domain.ports.curriculum_extractor import ICurriculumExtractor
from src.shared.logger import get_logger
from src.shared.result import Err, Ok, Result

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Bloom verb dictionaries (heuristic, no LLM)
# ---------------------------------------------------------------------------

_BLOOM_VERBS: dict[str, str] = {
    # Remember
    "nhớ": "remember", "liệt kê": "remember", "trình bày": "remember",
    "mô tả": "remember", "định nghĩa": "remember", "recall": "remember",
    "list": "remember", "define": "remember", "describe": "remember",
    # Understand
    "hiểu": "understand", "giải thích": "understand", "phân biệt": "understand",
    "tóm tắt": "understand", "explain": "understand", "summarize": "understand",
    "distinguish": "understand", "interpret": "understand",
    # Apply
    "áp dụng": "apply", "thực hiện": "apply", "sử dụng": "apply",
    "vận dụng": "apply", "apply": "apply", "use": "apply", "implement": "apply",
    "execute": "apply", "solve": "apply",
    # Analyze
    "phân tích": "analyze", "so sánh": "analyze", "phân loại": "analyze",
    "analyze": "analyze", "compare": "analyze", "classify": "analyze",
    "differentiate": "analyze", "examine": "analyze",
    # Evaluate
    "đánh giá": "evaluate", "nhận xét": "evaluate", "phê bình": "evaluate",
    "evaluate": "evaluate", "assess": "evaluate", "judge": "evaluate",
    "critique": "evaluate",
    # Create
    "thiết kế": "create", "xây dựng": "create", "tạo ra": "create",
    "đề xuất": "create", "design": "create", "create": "create",
    "build": "create", "construct": "create", "develop": "create",
    "formulate": "create",
}


def _infer_bloom(text: str) -> str | None:
    lower = text.lower()
    for verb, level in _BLOOM_VERBS.items():
        if verb in lower:
            return level
    return None


# ---------------------------------------------------------------------------
# Regex patterns (verified on CO3115 format)
# ---------------------------------------------------------------------------

_RE_COURSE_CODE = re.compile(r"Mã học phần.*?:\s*(?P<code>[A-Z]{2}\d{4})", re.IGNORECASE)
_RE_TITLE_VI = re.compile(r"Tên học phần\s*\(Tiếng Việt\)[^:]*:\s*(.+)")
_RE_TITLE_EN = re.compile(r"Tên học phần\s*\(Tiếng Anh\)[^:]*:\s*(.+)")
_RE_CREDITS = re.compile(r"Số tín chỉ\s*[:\-]\s*(\d+)")
_RE_SEMESTER = re.compile(r"Học kỳ\s*[:\-]\s*(.+?)(?:\n|$)")

# LO pattern: "L.O.3.5 - Phân tích ...\n(Analyze ...)"
_RE_LO = re.compile(
    r"L\.O\.(?P<code>\d+(?:\.\d+)?)\s*[-–]\s*(?P<vi>[^\n(]+?)\s*\n\s*\((?P<en>[^)]+)\)",
    re.MULTILINE,
)
# Fallback: LO without English on same line
_RE_LO_VI_ONLY = re.compile(
    r"L\.O\.(?P<code>\d+(?:\.\d+)?)\s*[-–]\s*(?P<vi>[^\n]+)",
    re.MULTILINE,
)

# Assessment: "A.O.4 - Kiểm tra giữa kỳ (Midterm)"  — greedy vi to avoid 1-char capture
_RE_ASSESSMENT = re.compile(
    r"A\.O\.(?P<code>\d+)\s*[-–]\s*(?P<vi>[^\n(]+?)(?:\s*\((?P<en>[^)]+)\))?(?:\s*[:\-]\s*(?P<weight>[\d,.]+)\s*%)?$",
    re.MULTILINE,
)

# LO ↔ Assessment mapping: "L.O.3.5 ... A.O.4" on same line/section
_RE_LO_AO_LINK = re.compile(
    r"L\.O\.(\d+\.\d+)\s.*?A\.O\.(\d+)",
    re.DOTALL,
)

# Chapter: "Chương 1.", "Chương 2:", or just "1. Introduction"
_RE_CHAPTER = re.compile(
    r"(?:Chương\s+)?(?P<num>\d+)\s*[.:\-]\s*(?P<title>[A-ZĐÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂẮẶẶẶẶẮẶẶẮẶẶẶẶẶ][^\n]{3,60})",
    re.MULTILINE,
)

# Section anchors
_SECTION_LO = re.compile(r"(?:4\.2|Chuẩn đầu ra học phần)", re.IGNORECASE)
_SECTION_ASSESSMENT = re.compile(r"(?:5\.2|5\.3|Phương pháp.*đánh giá|Hình thức.*đánh giá)", re.IGNORECASE)
_SECTION_LECTURES = re.compile(r"(?:6\.|Nội dung chi tiết)", re.IGNORECASE)


@dataclass
class _ParseState:
    full_text: str
    course_code: str = ""
    title_vi: str = ""
    title_en: str | None = None
    credits: int | None = None
    semester: str | None = None
    warnings: list[str] = field(default_factory=list)


class DcmhExtractor(ICurriculumExtractor):
    """Structural-only DCMH extractor sử dụng regex CDIO của BKHCM."""

    def extract(
        self,
        parsed_doc: ParsedDocument,
        course_id_hint: str | None = None,
    ) -> Result[Curriculum, Exception]:
        full_text = "\n".join(s.content for s in parsed_doc.sections)
        state = _ParseState(full_text=full_text)

        self._extract_header(state)
        if not state.course_code:
            if course_id_hint:
                state.course_code = course_id_hint
                state.warnings.append("course_code inferred from hint")
            else:
                return Err(ProcessingError("Cannot extract course code from DCMH — structural parse failed"))

        course_id = course_id_hint or state.course_code

        los, lo_conf = self._extract_los(state, course_id)
        assessments, assess_conf = self._extract_assessments(state, course_id)
        chapters, chap_conf = self._extract_chapters(state, course_id)
        lo_ao_links = self._extract_lo_ao_links(state, los, assessments)

        # confidence = average of per-section confidences
        conf_values = [v for v in [lo_conf, assess_conf, chap_conf] if v is not None]
        confidence = sum(conf_values) / len(conf_values) if conf_values else 0.5

        if confidence < 0.5:
            return Err(
                ProcessingError(
                    f"DCMH extraction confidence {confidence:.2f} below threshold 0.5 — "
                    f"warnings: {state.warnings}"
                )
            )

        course = Course(
            course_id=course_id,
            code=state.course_code,
            title_vi=state.title_vi or state.course_code,
            title_en=state.title_en,
            credits=state.credits,
            semester=state.semester,
        )

        curriculum = Curriculum(
            course=course,
            chapters=tuple(chapters),
            learning_outcomes=tuple(los),
            assessments=tuple(assessments),
            lo_assessment_links=tuple(lo_ao_links),
            source_document_id=parsed_doc.document.id,
            extraction_confidence=confidence,
        )

        logger.info(
            "dcmh_extractor.done",
            course_id=course_id,
            los=len(los),
            assessments=len(assessments),
            chapters=len(chapters),
            confidence=round(confidence, 3),
            warnings=state.warnings,
        )
        return Ok(curriculum)

    # ------------------------------------------------------------------
    # Header extraction
    # ------------------------------------------------------------------

    def _extract_header(self, state: _ParseState) -> None:
        m = _RE_COURSE_CODE.search(state.full_text)
        if m:
            state.course_code = m.group("code").strip()

        m = _RE_TITLE_VI.search(state.full_text)
        if m:
            state.title_vi = m.group(1).strip()

        m = _RE_TITLE_EN.search(state.full_text)
        if m:
            state.title_en = m.group(1).strip()

        m = _RE_CREDITS.search(state.full_text)
        if m:
            try:
                state.credits = int(m.group(1))
            except ValueError:
                pass

        m = _RE_SEMESTER.search(state.full_text)
        if m:
            state.semester = m.group(1).strip()

    # ------------------------------------------------------------------
    # LO extraction
    # ------------------------------------------------------------------

    def _extract_los(
        self, state: _ParseState, course_id: str
    ) -> tuple[list[LearningOutcome], float]:
        # Locate LO section
        anchor = _SECTION_LO.search(state.full_text)
        section_text = state.full_text[anchor.start():] if anchor else state.full_text

        los: list[LearningOutcome] = []
        seen_codes: set[str] = set()

        for m in _RE_LO.finditer(section_text):
            code = m.group("code").strip()
            vi = m.group("vi").strip()
            en = m.group("en").strip()
            if code in seen_codes:
                continue
            seen_codes.add(code)
            los.append(self._build_lo(course_id, code, vi, en))

        if not los:
            # Fallback: VI-only pattern
            for m in _RE_LO_VI_ONLY.finditer(section_text):
                code = m.group("code").strip()
                vi = m.group("vi").strip()
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                los.append(self._build_lo(course_id, code, vi, None))
            if los:
                state.warnings.append("LO English statements not found — using VI-only fallback")

        if not los:
            state.warnings.append("No LOs extracted")
            return [], 0.3

        return los, 0.9

    def _build_lo(
        self, course_id: str, code: str, vi: str, en: str | None
    ) -> LearningOutcome:
        parts = code.split(".")
        parent_code = ".".join(parts[:-1]) if len(parts) >= 2 else None
        bloom = _infer_bloom(vi) or (en and _infer_bloom(en))
        return LearningOutcome(
            lo_id=f"{course_id}:L.O.{code}",
            code=f"L.O.{code}",
            parent_code=f"L.O.{parent_code}" if parent_code else None,
            statement_vi=vi,
            statement_en=en,
            bloom_level=bloom,
            cdio_level=None,
        )

    # ------------------------------------------------------------------
    # Assessment extraction
    # ------------------------------------------------------------------

    def _extract_assessments(
        self, state: _ParseState, course_id: str
    ) -> tuple[list[Assessment], float]:
        anchor = _SECTION_ASSESSMENT.search(state.full_text)
        section_text = state.full_text[anchor.start():] if anchor else state.full_text

        assessments: list[Assessment] = []
        seen: set[str] = set()

        for m in _RE_ASSESSMENT.finditer(section_text):
            code = m.group("code").strip()
            if code in seen:
                continue
            seen.add(code)
            vi = m.group("vi").strip()
            en = m.group("en").strip() if m.group("en") else None
            weight_str = m.group("weight") if m.group("weight") else None
            weight = float(weight_str.replace(",", ".")) / 100.0 if weight_str else None
            category = self._infer_category(vi, en or "")
            assessments.append(Assessment(
                assessment_id=f"{course_id}:A.O.{code}",
                code=f"A.O.{code}",
                name_vi=vi,
                name_en=en,
                category=category,
                weight=weight,
            ))

        if not assessments:
            state.warnings.append("No assessments extracted")
            return [], 0.3

        return assessments, 0.9

    def _infer_category(self, vi: str, en: str) -> str:
        combined = (vi + " " + en).lower()
        if "giữa kỳ" in combined or "midterm" in combined:
            return "midterm"
        if "cuối kỳ" in combined or "final" in combined:
            return "final"
        if "dự án" in combined or "project" in combined:
            return "project"
        if "nhóm" in combined or "group" in combined:
            return "group_quiz"
        return "quiz"

    # ------------------------------------------------------------------
    # Chapter extraction
    # ------------------------------------------------------------------

    def _extract_chapters(
        self, state: _ParseState, course_id: str
    ) -> tuple[list[Chapter], float]:
        anchor = _SECTION_LECTURES.search(state.full_text)
        section_text = state.full_text[anchor.start():] if anchor else state.full_text

        chapters: list[Chapter] = []
        seen: set[str] = set()

        for idx, m in enumerate(_RE_CHAPTER.finditer(section_text)):
            num = m.group("num")
            title = m.group("title").strip()
            if num in seen:
                continue
            seen.add(num)
            chapters.append(Chapter(
                chapter_id=f"{course_id}:CH{num}",
                code=num,
                title=title,
                order_index=int(num),
            ))

        if not chapters:
            state.warnings.append("No chapters extracted")
            return [], 0.5

        return chapters, 0.85

    # ------------------------------------------------------------------
    # LO ↔ Assessment links
    # ------------------------------------------------------------------

    def _extract_lo_ao_links(
        self,
        state: _ParseState,
        los: list[LearningOutcome],
        assessments: list[Assessment],
    ) -> list[tuple[str, str]]:
        lo_by_code = {lo.code: lo for lo in los}
        assess_by_code = {a.code: a for a in assessments}

        links: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        anchor = _SECTION_ASSESSMENT.search(state.full_text)
        section_text = state.full_text[anchor.start():] if anchor else state.full_text

        for m in _RE_LO_AO_LINK.finditer(section_text):
            lo_code = f"L.O.{m.group(1)}"
            ao_code = f"A.O.{m.group(2)}"
            lo = lo_by_code.get(lo_code)
            ao = assess_by_code.get(ao_code)
            if lo and ao:
                pair = (lo.lo_id, ao.assessment_id)
                if pair not in seen:
                    seen.add(pair)
                    links.append(pair)

        return links

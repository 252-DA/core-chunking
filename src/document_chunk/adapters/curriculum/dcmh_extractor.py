"""
DcmhExtractor — trích đề cương môn học (DCMH, định dạng CDIO của BKHCM).

Thiết kế:
  - Lấy **cấu trúc chương trình** làm trung tâm: mục → thực thể → quan hệ.
  - Đọc mục 5.2 / 5.3 / 6 từ **bảng thật** (hàng/cột, nối hàng qua trang), không
    từ chuỗi đã làm phẳng. Ba con số khác nghĩa (số buổi, số chương, số mục
    trong giáo trình) chỉ phân biệt được khi còn cột.
  - Không chấm một điểm confidence trung bình. Thay vào đó chạy các kiểm tra
    cụ thể và trả về ``ExtractionIssue`` để giảng viên soát.
  - Bloom/CDIO không có trong tài liệu ⇒ để None hoặc đánh dấu ``inferred``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from document_chunk.adapters.curriculum.syllabus_layout import (
    LayoutTable,
    PyMuPdfSyllabusReader,
    SyllabusLayout,
)
from document_chunk.domain.entities.curriculum import (
    Assessment,
    Chapter,
    ChapterLOLink,
    Course,
    CourseGoal,
    Curriculum,
    ExtractionIssue,
    LearningOutcome,
    LOAssessmentLink,
    SourceRef,
    SyllabusRow,
)
from document_chunk.domain.entities.document import ParsedDocument
from document_chunk.domain.exceptions import ProcessingError
from document_chunk.domain.ports.curriculum_extractor import ICurriculumExtractor
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Bloom — chỉ là gợi ý; tài liệu không ghi Bloom nên luôn mang provenance
# "inferred". Khớp theo động từ xuất hiện SỚM NHẤT trong câu, không theo thứ tự
# từ điển (thứ tự từ điển làm "Tổng hợp ... phân tích ... sử dụng" ra "apply").
# ---------------------------------------------------------------------------

_BLOOM_VERBS: dict[str, str] = {
    "nhớ": "remember", "liệt kê": "remember", "trình bày": "remember",
    "mô tả": "remember", "định nghĩa": "remember",
    "hiểu": "understand", "giải thích": "understand", "phân biệt": "understand",
    "tóm tắt": "understand", "nhận diện": "understand",
    "áp dụng": "apply", "thực hiện": "apply", "sử dụng": "apply", "vận dụng": "apply",
    "phân tích": "analyze", "so sánh": "analyze", "phân loại": "analyze",
    "đánh giá": "evaluate", "nhận xét": "evaluate", "phê bình": "evaluate",
    "giao tiếp": "apply", "quản lý": "apply", "lập kế hoạch": "apply",
    "tổng hợp": "create", "thiết kế": "create", "xây dựng": "create",
    "tạo ra": "create", "đề xuất": "create",
}


def _infer_bloom(text: str) -> str | None:
    lower = text.lower()
    best: tuple[int, str] | None = None
    for verb, level in _BLOOM_VERBS.items():
        pos = lower.find(verb)
        if pos >= 0 and (best is None or pos < best[0]):
            best = (pos, level)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------

_RE_SECTION = re.compile(r"^(?P<num>\d+(?:\.\d+)*)\.\s+(?P<title>\S.*)$", re.MULTILINE)
_RE_LO_CODE = re.compile(r"L\.?\s?O\.(?P<code>\d+(?:\.\d+)*)")
_RE_AO_CODE = re.compile(r"A\.?\s?O\.(?P<code>\d+(?:\.\d+)*)")
# Ô cột phải của mục 6: "L.O.2.2 [ A.O.1 , A.O.1.1 ]"
_RE_ROW_LINK = re.compile(
    r"L\.O\.(?P<lo>\d+(?:\.\d+)*)\s*\[(?P<aos>[^\]]*)\]"
)
_RE_CHAPTER_CELL = re.compile(r"Chương\s*(?P<num>\d+)", re.IGNORECASE)
_RE_SESSION_CELL = re.compile(r"^(?P<num>\d+)$")
_RE_LEADING_NUM = re.compile(r"^(?P<num>\d+)\s+(?P<title>\S.*)$")

_RE_COURSE_CODE = re.compile(r"Mã học phần[^:]*:\s*(?P<code>[A-Z]{2}\d{3,5})")
_RE_TITLE = re.compile(r"Tên học phần\s*:\s*(?P<vi>[^(\n]+?)\s*\(\s*(?P<en>[^)\n]+?)\s*\)")
_RE_CREDITS = re.compile(r"Số tín chỉ[^:]*:\s*(?P<n>\d+)")
_RE_SEMESTER = re.compile(r"Học kỳ áp dụng[^:]*:\s*(?P<v>\S+)")
_RE_LO_YEAR = re.compile(r"Năm chuẩn đầu ra áp dụng[^:]*:\s*(?P<v>\d{4})")
_RE_SYLLABUS_VERSION = re.compile(r"Editing version\)\s*:\s*(?P<v>\S+)")

_WS = re.compile(r"\s+")
# BOM và các ký tự zero-width lọt vào từ text layer của PDF.
_INVISIBLE = re.compile(r"[\ufeff\u200b\u200c\u200d\u2060]")


def _norm(text: str) -> str:
    return _WS.sub(" ", _INVISIBLE.sub("", text or "")).strip()


def _split_bilingual(block: str) -> tuple[str, str | None]:
    """
    Tách "VI ... \n(EN ...)" thành (vi, en).

    Bản tiếng Anh trong DCMH nằm trong ngoặc, bắt đầu ở đầu một dòng và chạy đến
    hết khối — kể cả khi khối bị ngắt qua hai trang. Chọn điểm cắt SỚM NHẤT mà
    phần còn lại là một nhóm ngoặc đóng kín; không có thì coi như thiếu bản dịch.
    """
    block = block.strip()
    for match in re.finditer(r"\n[ \t]*\(", block):
        tail = block[match.start():].strip()
        if tail.startswith("(") and tail.endswith(")") and tail.count("(") == tail.count(")"):
            return _norm(block[: match.start()]), _norm(tail[1:-1])
    # Bản EN nằm cùng dòng, trong ngoặc ở cuối (kiểu ô bảng mục 5.2).
    inline = re.search(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*$", block)
    if inline and inline.start() > 0:
        return _norm(block[: inline.start()]), _norm(inline.group(1))
    return _norm(block), None


@dataclass
class _Ctx:
    layout: SyllabusLayout
    sections: dict[str, tuple[int, int]]
    issues: list[ExtractionIssue] = field(default_factory=list)

    def add(self, code: str, severity: str, message: str, source: SourceRef | None = None) -> None:
        self.issues.append(ExtractionIssue(code=code, severity=severity, message=message, source=source))

    def section_text(self, number: str) -> str:
        span = self.sections.get(number)
        return self.layout.text[span[0]: span[1]] if span else ""

    def section_page(self, number: str) -> int | None:
        span = self.sections.get(number)
        return self.layout.page_at(span[0]) if span else None


class DcmhExtractor(ICurriculumExtractor):
    """Trích DCMH theo cấu trúc mục + bảng, kèm bằng chứng nguồn."""

    def __init__(self, reader: PyMuPdfSyllabusReader | None = None) -> None:
        self._reader = reader or PyMuPdfSyllabusReader()

    # ------------------------------------------------------------------

    def extract(
        self,
        parsed_doc: ParsedDocument,
        course_id_hint: str | None = None,
    ) -> Result[Curriculum, Exception]:
        layout_result = self._build_layout(parsed_doc)
        if layout_result.is_err():
            return Err(layout_result.error)
        layout, layout_issues = layout_result.unwrap()

        ctx = _Ctx(layout=layout, sections=self._index_sections(layout))
        ctx.issues.extend(layout_issues)

        course = self._extract_course(ctx, course_id_hint)
        if course is None:
            return Err(ProcessingError("Cannot extract course code from DCMH"))

        goals = self._extract_goals(ctx)
        los = self._extract_los(ctx, course.course_id)
        assessments = self._extract_assessments(ctx, course.course_id)
        rows = self._extract_syllabus_rows(ctx)
        chapters = self._chapters_from_rows(ctx, course.course_id, rows)
        chapter_lo_links = self._chapter_lo_links(rows)
        lo_ao_links = self._lo_assessment_links(ctx, rows)

        self._validate(ctx, goals, los, assessments, chapters, rows, chapter_lo_links, lo_ao_links)

        curriculum = Curriculum(
            course=course,
            chapters=tuple(chapters),
            learning_outcomes=tuple(los),
            assessments=tuple(assessments),
            lo_assessment_links=tuple(lo_ao_links),
            goals=tuple(goals),
            sessions=tuple(rows),
            chapter_lo_links=tuple(chapter_lo_links),
            issues=tuple(ctx.issues),
            source_document_id=parsed_doc.document.id,
        )

        logger.info(
            "dcmh_extractor.done",
            course_id=course.course_id,
            los=len(los),
            assessments=len(assessments),
            chapters=len(chapters),
            sessions=len(rows),
            chapter_lo_links=len(chapter_lo_links),
            lo_ao_links=len(lo_ao_links),
            errors=len(curriculum.blocking_issues),
            warnings=sum(1 for i in ctx.issues if i.severity == "warning"),
        )
        return Ok(curriculum)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_layout(
        self, parsed_doc: ParsedDocument
    ) -> Result[tuple[SyllabusLayout, list[ExtractionIssue]], Exception]:
        path = parsed_doc.document.path
        if path is not None and Path(path).suffix.lower() == ".pdf" and Path(path).exists():
            result = self._reader.read(Path(path))
            if result.is_err():
                return Err(result.error)
            return Ok((result.unwrap(), []))

        # Không có PDF gốc → chỉ còn text đã làm phẳng. Mục 4.x và 5.x vẫn đọc
        # được, nhưng bảng mục 6 thì không: cấu trúc cột đã mất.
        from document_chunk.adapters.curriculum.syllabus_layout import LayoutPage

        text = "\n".join(s.content for s in parsed_doc.sections)
        layout = SyllabusLayout((LayoutPage(number=1, text=text, tables=()),))
        issue = ExtractionIssue(
            code="layout.text_only",
            severity="warning",
            message=(
                "Không có PDF gốc để đọc bảng; chỉ dùng text đã làm phẳng. "
                "Quan hệ chương–LO ở mục 6 sẽ không trích được."
            ),
        )
        return Ok((layout, [issue]))

    def _index_sections(self, layout: SyllabusLayout) -> dict[str, tuple[int, int]]:
        """Mục N → (start, end) trong layout.text. Neo theo tiêu đề mục ở đầu dòng."""
        marks = [(m.group("num"), m.start()) for m in _RE_SECTION.finditer(layout.text)]
        spans: dict[str, tuple[int, int]] = {}
        for idx, (number, start) in enumerate(marks):
            # Mục kết thúc ở tiêu đề kế tiếp KHÔNG phải mục con của nó.
            end = len(layout.text)
            for later_number, later_start in marks[idx + 1:]:
                if not later_number.startswith(number + "."):
                    end = later_start
                    break
            if number not in spans:      # lần xuất hiện đầu tiên là tiêu đề thật
                spans[number] = (start, end)
        return spans

    # ------------------------------------------------------------------
    # Mục 1 — thông tin học phần
    # ------------------------------------------------------------------

    def _extract_course(self, ctx: _Ctx, course_id_hint: str | None) -> Course | None:
        text = ctx.layout.text
        m = _RE_COURSE_CODE.search(text)
        code = m.group("code") if m else (course_id_hint or "")
        if not code:
            return None
        if m is None:
            ctx.add("course.code_from_hint", "warning",
                    "Không đọc được mã học phần trong tài liệu; dùng giá trị caller truyền vào.")

        title_vi, title_en = code, None
        mt = _RE_TITLE.search(text)
        if mt:
            title_vi, title_en = _norm(mt.group("vi")), _norm(mt.group("en"))
        else:
            ctx.add("course.title_missing", "warning", "Không đọc được tên học phần.")

        credits = int(mc.group("n")) if (mc := _RE_CREDITS.search(text)) else None
        semester = ms.group("v") if (ms := _RE_SEMESTER.search(text)) else None
        lo_year = my.group("v") if (my := _RE_LO_YEAR.search(text)) else None
        version = mv.group("v") if (mv := _RE_SYLLABUS_VERSION.search(text)) else None
        if version is None:
            ctx.add("course.syllabus_version_missing", "warning",
                    "Không đọc được mã phiên bản đề cương (mục 8).")

        return Course(
            course_id=course_id_hint or code,
            code=code,
            title_vi=title_vi,
            title_en=title_en,
            credits=credits,
            semester=semester,
            syllabus_version=version,
            lo_year=lo_year,
        )

    # ------------------------------------------------------------------
    # Mục 4.1 — mục tiêu (KHÔNG phải chuẩn đầu ra)
    # ------------------------------------------------------------------

    def _extract_goals(self, ctx: _Ctx) -> list[CourseGoal]:
        text = ctx.section_text("4.1")
        if not text:
            return []
        page = ctx.section_page("4.1")
        source = SourceRef(section="4.1", page=page)

        # Mục 4.1 liệt kê bản Việt trước rồi bản Anh sau, cùng bộ mã.
        vi: dict[str, str] = {}
        en: dict[str, str] = {}
        marks = list(_RE_LO_CODE.finditer(text))
        for idx, m in enumerate(marks):
            code = f"L.O.{m.group('code')}"
            end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
            body = _norm(text[m.end(): end]).lstrip("-–").strip()
            if not body:
                continue
            target = vi if code not in vi else en
            target.setdefault(code, body)

        return [
            CourseGoal(code=code, statement_vi=statement, statement_en=en.get(code), source=source)
            for code, statement in vi.items()
        ]

    # ------------------------------------------------------------------
    # Mục 4.2 — chuẩn đầu ra
    # ------------------------------------------------------------------

    def _extract_los(self, ctx: _Ctx, course_id: str) -> list[LearningOutcome]:
        text = ctx.section_text("4.2")
        if not text:
            ctx.add("lo.section_missing", "error", "Không tìm thấy mục 4.2 (chuẩn đầu ra).")
            return []
        page = ctx.section_page("4.2")

        marks = list(_RE_LO_CODE.finditer(text))
        los: list[LearningOutcome] = []
        seen: set[str] = set()
        for idx, m in enumerate(marks):
            code = f"L.O.{m.group('code')}"
            end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
            block = text[m.end(): end].lstrip()
            block = re.sub(r"^[-–]\s*", "", block)
            if not block.strip():
                continue
            if code in seen:
                ctx.add("lo.duplicate_code", "warning",
                        f"{code} xuất hiện nhiều lần trong mục 4.2; giữ lần đầu.",
                        SourceRef(section="4.2", page=page))
                continue
            seen.add(code)

            statement_vi, statement_en = _split_bilingual(block)
            lo_page = ctx.layout.page_at(ctx.sections["4.2"][0] + m.start())
            source = SourceRef(section="4.2", page=lo_page)
            if statement_en is None:
                ctx.add("lo.missing_english", "warning",
                        f"{code} không có bản tiếng Anh.", source)

            parts = m.group("code").split(".")
            parent = f"L.O.{'.'.join(parts[:-1])}" if len(parts) > 1 else None
            los.append(LearningOutcome(
                lo_id=f"{course_id}:{code}",
                code=code,
                parent_code=parent,
                statement_vi=statement_vi,
                statement_en=statement_en,
                bloom_level=_infer_bloom(statement_vi),
                bloom_provenance="inferred",
                cdio_level=None,
                cdio_provenance="inferred",
                source=source,
            ))

        if not los:
            ctx.add("lo.none_extracted", "error", "Không trích được chuẩn đầu ra nào từ mục 4.2.")
        return los

    # ------------------------------------------------------------------
    # Mục 5.2 — hoạt động đánh giá
    # ------------------------------------------------------------------

    def _extract_assessments(self, ctx: _Ctx, course_id: str) -> list[Assessment]:
        table = self._find_table(ctx, ("Loại hoạt", "Activity"))
        rows: list[tuple[str, str, SourceRef]] = []
        if table is not None:
            for ri, row in enumerate(table.rows[1:], start=1):
                if len(row) < 2 or not _RE_AO_CODE.search(row[1]):
                    continue
                rows.append((row[0], row[1],
                             SourceRef(section="5.2", page=table.page,
                                       locator=table.locator(ri, 1))))
        else:
            ctx.add("assessment.table_missing", "warning",
                    "Không đọc được bảng mục 5.2; thử trích từ text.")
            text = ctx.section_text("5.2")
            page = ctx.section_page("5.2")
            marks = list(_RE_AO_CODE.finditer(text))
            for idx, m in enumerate(marks):
                end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
                rows.append(("", text[m.start(): end],
                             SourceRef(section="5.2", page=page)))

        assessments: list[Assessment] = []
        seen: set[str] = set()
        for activity_cell, body, source in rows:
            m = _RE_AO_CODE.search(body)
            if m is None:
                continue
            code = f"A.O.{m.group('code')}"
            if code in seen:
                continue
            seen.add(code)

            rest = re.sub(r"^[-–]\s*", "", body[m.end():].lstrip())
            name_vi, name_en = _split_bilingual(rest)
            activity_type = None
            if activity_cell:
                at = re.match(r"\s*([A-Z]{2,4})\s*-", _norm(activity_cell))
                activity_type = at.group(1) if at else None

            parts = m.group("code").split(".")
            parent = f"A.O.{'.'.join(parts[:-1])}" if len(parts) > 1 else None
            assessments.append(Assessment(
                assessment_id=f"{course_id}:{code}",
                code=code,
                name_vi=name_vi,
                parent_code=parent,
                name_en=name_en,
                activity_type=activity_type,
                category=self._infer_category(activity_cell, name_vi, name_en or ""),
                weight=None,
                weight_provenance="inferred",
                source=source,
            ))

        if not assessments:
            ctx.add("assessment.none_extracted", "error",
                    "Không trích được hoạt động đánh giá nào từ mục 5.2.")
        return assessments

    def _infer_category(self, activity_cell: str, vi: str, en: str) -> str:
        combined = _norm(f"{activity_cell} {vi} {en}").lower()
        if "giữa kỳ" in combined or "midterm" in combined:
            return "midterm"
        if "cuối kỳ" in combined or "final exam" in combined:
            return "final"
        if "nhóm" in combined or "group" in combined:
            return "group_quiz"
        if "dự án" in combined or "project" in combined:
            return "project"
        return "quiz"

    # ------------------------------------------------------------------
    # Mục 6 — bảng nội dung chi tiết
    # ------------------------------------------------------------------

    def _extract_syllabus_rows(self, ctx: _Ctx) -> list[SyllabusRow]:
        tables = [t for t in ctx.layout.tables if self._is_session_table(t)]
        if not tables:
            # Lỗi chặn, không phải cảnh báo: mất bảng là mất toàn bộ căn cứ cho
            # quan hệ chương–LO. Đoán từ mã LO chính là cái đang phải bỏ.
            ctx.add("session.table_missing", "error",
                    "Không đọc được bảng mục 6; không có căn cứ nào cho quan hệ chương–LO.")
            return []

        rows: list[SyllabusRow] = []
        order = 0
        for table in tables:
            for ri, raw in enumerate(table.rows):
                if len(raw) < 3:
                    continue
                label, content, activity = _norm(raw[0]), raw[1], raw[2]
                if self._is_header_row(label, content):
                    continue
                if not label:
                    # Ô cột Buổi rỗng → hàng nối tiếp của hàng trang trước.
                    if rows:
                        rows[-1] = self._append_continuation(rows[-1], content, activity)
                    continue

                order += 1
                source = SourceRef(section="6", page=table.page, locator=table.locator(ri))
                chapter_code = None
                session_no = None
                if (mc := _RE_CHAPTER_CELL.search(label)):
                    chapter_code = mc.group("num")
                elif (ms := _RE_SESSION_CELL.match(label)):
                    session_no = int(ms.group("num"))
                else:
                    ctx.add("session.unreadable_label", "warning",
                            f"Không hiểu ô cột Buổi: {label!r}.", source)

                title_vi, title_en = self._row_title(content, chapter_code)
                lo_codes, ao_codes = self._row_links(activity)

                rows.append(SyllabusRow(
                    order_index=order,
                    session_no=session_no if session_no is not None else order,
                    chapter_code=chapter_code,
                    title_vi=title_vi,
                    title_en=title_en,
                    lo_codes=lo_codes,
                    assessment_codes=ao_codes,
                    source=source,
                ))

                if session_no is not None and session_no != order:
                    ctx.add("session.number_mismatch", "warning",
                            f"Buổi ghi {session_no} nhưng đứng ở vị trí hàng {order}.", source)
        return rows

    def _is_session_table(self, table: LayoutTable) -> bool:
        if not table.rows:
            return False
        head = _norm(" ".join(table.rows[0])).lower()
        return "buổi" in head or "session" in head

    def _is_header_row(self, label: str, content: str) -> bool:
        low = f"{label} {_norm(content)}".lower()
        return "buổi" in low and "session" in low and "nội dung" in low

    def _append_continuation(self, row: SyllabusRow, content: str, activity: str) -> SyllabusRow:
        """Nối phần thân của hàng bị ngắt qua trang; không tạo hàng mới."""
        lo_codes, ao_codes = self._row_links(activity)
        return SyllabusRow(
            order_index=row.order_index,
            session_no=row.session_no,
            chapter_code=row.chapter_code,
            title_vi=row.title_vi,
            title_en=row.title_en,
            lo_codes=tuple(dict.fromkeys(row.lo_codes + lo_codes)),
            assessment_codes=tuple(dict.fromkeys(row.assessment_codes + ao_codes)),
            source=row.source,
        )

    def _row_title(self, content: str, chapter_code: str | None) -> tuple[str, str | None]:
        lines = [ln.strip() for ln in (content or "").splitlines() if ln.strip()]
        if not lines:
            return "", None
        first = lines[0]
        if (m := _RE_LEADING_NUM.match(first)):
            title_vi = _norm(m.group("title"))
        else:
            title_vi = _norm(first)

        title_en = None
        if chapter_code:
            # Bản tiếng Anh của chương mở đầu bằng "(<số chương> <Title>".
            me = re.search(rf"\(\s*{re.escape(chapter_code)}\s+([^\n]+)", content or "")
            if me:
                title_en = _norm(me.group(1))
        if title_en is None:
            for ln in lines[1:]:
                if ln.startswith("("):
                    title_en = _norm(ln.lstrip("(").rstrip(")"))
                    break
        return title_vi, title_en

    def _row_links(self, activity: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        lo_codes: list[str] = []
        ao_codes: list[str] = []
        for m in _RE_ROW_LINK.finditer(activity or ""):
            lo_codes.append(f"L.O.{m.group('lo')}")
            for ao in _RE_AO_CODE.finditer(m.group("aos")):
                ao_codes.append(f"A.O.{ao.group('code')}")
        return tuple(dict.fromkeys(lo_codes)), tuple(dict.fromkeys(ao_codes))

    # ------------------------------------------------------------------
    # Chương và quan hệ
    # ------------------------------------------------------------------

    def _chapters_from_rows(
        self, ctx: _Ctx, course_id: str, rows: list[SyllabusRow]
    ) -> list[Chapter]:
        chapters: list[Chapter] = []
        seen: set[str] = set()
        for row in rows:
            if row.chapter_code is None or row.chapter_code in seen:
                continue
            seen.add(row.chapter_code)
            chapters.append(Chapter(
                chapter_id=f"{course_id}:CH{row.chapter_code}",
                code=row.chapter_code,
                title=row.title_vi,
                title_en=row.title_en,
                order_index=int(row.chapter_code),
                source=row.source,
            ))
        return sorted(chapters, key=lambda c: c.order_index)

    def _chapter_lo_links(self, rows: list[SyllabusRow]) -> list[ChapterLOLink]:
        links: dict[tuple[str, str], ChapterLOLink] = {}
        for row in rows:
            if row.chapter_code is None:
                continue
            for lo_code in row.lo_codes:
                key = (row.chapter_code, lo_code)
                links.setdefault(key, ChapterLOLink(
                    chapter_code=row.chapter_code,
                    lo_code=lo_code,
                    provenance="extracted",
                    source=row.source,
                ))
        return list(links.values())

    def _lo_assessment_links(
        self, ctx: _Ctx, rows: list[SyllabusRow]
    ) -> list[LOAssessmentLink]:
        """
        Hai nguồn: mục 5.3 (phát biểu chung) và mục 6 (theo từng hàng).

        Giữ riêng, vì mục 6 khẳng định theo ngữ cảnh — Chương 10 chỉ đánh giá
        L.O.3 qua A.O.1, trong khi mục 5.3 nói L.O.3 gắn cả A.O.1 và A.O.2.
        Làm phẳng thành một cạnh duy nhất sẽ mất khác biệt đó.
        """
        links: list[LOAssessmentLink] = []
        seen: set[tuple[str, str, int | None]] = set()

        for lo_code, ao_code, source in self._section_5_3_pairs(ctx):
            key = (lo_code, ao_code, None)
            if key in seen:
                continue
            seen.add(key)
            links.append(LOAssessmentLink(
                lo_code=lo_code, assessment_code=ao_code,
                row_order_index=None, chapter_code=None,
                provenance="extracted", source=source,
            ))

        for row in rows:
            for lo_code in row.lo_codes:
                for ao_code in row.assessment_codes:
                    key = (lo_code, ao_code, row.order_index)
                    if key in seen:
                        continue
                    seen.add(key)
                    links.append(LOAssessmentLink(
                        lo_code=lo_code, assessment_code=ao_code,
                        row_order_index=row.order_index,
                        chapter_code=row.chapter_code,
                        provenance="extracted", source=row.source,
                    ))
        return links

    def _section_5_3_pairs(self, ctx: _Ctx) -> list[tuple[str, str, SourceRef]]:
        pairs: list[tuple[str, str, SourceRef]] = []
        table = self._find_table(ctx, ("Chuẩn đầu ra chi tiết", "Learning outcome"))
        if table is not None:
            for ri, row in enumerate(table.rows[1:], start=1):
                if len(row) < 2:
                    continue
                lo_cell = row[-2]
                ao_cell = row[-1]
                mlo = _RE_LO_CODE.search(lo_cell)
                if mlo is None:
                    continue
                lo_code = f"L.O.{mlo.group('code')}"
                source = SourceRef(section="5.3", page=table.page, locator=table.locator(ri))
                for mao in _RE_AO_CODE.finditer(ao_cell):
                    pairs.append((lo_code, f"A.O.{mao.group('code')}", source))
            return pairs

        # Fallback text: cắt theo từng mã LO rồi gom mã AO trong khối.
        text = ctx.section_text("5.3")
        if not text:
            return pairs
        page = ctx.section_page("5.3")
        source = SourceRef(section="5.3", page=page)
        marks = list(_RE_LO_CODE.finditer(text))
        for idx, m in enumerate(marks):
            end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
            lo_code = f"L.O.{m.group('code')}"
            for mao in _RE_AO_CODE.finditer(text[m.end(): end]):
                pairs.append((lo_code, f"A.O.{mao.group('code')}", source))
        return pairs

    def _find_table(self, ctx: _Ctx, needles: tuple[str, ...]) -> LayoutTable | None:
        for table in ctx.layout.tables:
            if not table.rows:
                continue
            head = _norm(" ".join(table.rows[0])).lower()
            if any(n.lower() in head for n in needles):
                return table
        return None

    # ------------------------------------------------------------------
    # Kiểm tra độ đầy đủ — thay cho điểm confidence
    # ------------------------------------------------------------------

    def _validate(
        self,
        ctx: _Ctx,
        goals: list[CourseGoal],
        los: list[LearningOutcome],
        assessments: list[Assessment],
        chapters: list[Chapter],
        rows: list[SyllabusRow],
        chapter_lo_links: list[ChapterLOLink],
        lo_ao_links: list[LOAssessmentLink],
    ) -> None:
        lo_codes = {lo.code for lo in los}
        ao_codes = {a.code for a in assessments}

        # 1. Mã được tham chiếu phải tồn tại.
        for link in chapter_lo_links:
            if link.lo_code not in lo_codes:
                ctx.add("ref.unknown_lo", "error",
                        f"Chương {link.chapter_code} tham chiếu {link.lo_code} "
                        f"nhưng mục 4.2 không định nghĩa mã này.", link.source)
        for link in lo_ao_links:
            if link.lo_code not in lo_codes:
                ctx.add("ref.unknown_lo", "error",
                        f"{link.lo_code} được tham chiếu nhưng mục 4.2 không định nghĩa.",
                        link.source)
            if link.assessment_code not in ao_codes:
                ctx.add("ref.unknown_ao", "error",
                        f"{link.assessment_code} được tham chiếu nhưng mục 5.2 không định nghĩa.",
                        link.source)

        # 2. LO con phải có cha; AO con cũng vậy.
        for lo in los:
            if lo.parent_code and lo.parent_code not in lo_codes:
                ctx.add("lo.orphan_parent", "error",
                        f"{lo.code} khai báo cha {lo.parent_code} nhưng mục 4.2 không có mã đó.",
                        lo.source)
        for a in assessments:
            if a.parent_code and a.parent_code not in ao_codes:
                ctx.add("assessment.orphan_parent", "error",
                        f"{a.code} khai báo cha {a.parent_code} nhưng mục 5.2 không có mã đó.",
                        a.source)

        # 3. LO lá được định nghĩa nhưng không chương nào dạy.
        parents = {lo.parent_code for lo in los if lo.parent_code}
        taught = {link.lo_code for link in chapter_lo_links}
        assessed = {link.lo_code for link in lo_ao_links}
        for lo in los:
            if lo.code in parents:
                continue      # LO cha không cần gắn chương trực tiếp
            if lo.code not in taught:
                ctx.add("lo.not_taught", "warning",
                        f"{lo.code} được định nghĩa ở mục 4.2 nhưng không hàng nào "
                        f"của mục 6 nhắc tới.", lo.source)
            if lo.code not in assessed:
                ctx.add("lo.not_assessed", "warning",
                        f"{lo.code} không gắn với hoạt động đánh giá nào.", lo.source)

        # 4. Số chương ở cột Buổi phải khớp số dẫn đầu ô nội dung.
        for row in rows:
            if row.chapter_code is None:
                continue
            if not row.title_vi:
                ctx.add("chapter.title_missing", "warning",
                        f"Chương {row.chapter_code} không đọc được tiêu đề.", row.source)

        # 5. Mục 4.1 dùng lại ký hiệu L.O.n với nội dung khác mục 4.2.
        lo_by_code = {lo.code: lo for lo in los}
        for goal in goals:
            other = lo_by_code.get(goal.code)
            if other is not None and _norm(other.statement_vi) != _norm(goal.statement_vi):
                ctx.add("goal.code_collision", "info",
                        f"{goal.code} ở mục 4.1 (mục tiêu) khác nội dung {goal.code} ở "
                        f"mục 4.2 (chuẩn đầu ra); lưu tách riêng.", goal.source)

        # 6. Bloom/CDIO không có trong tài liệu này.
        if los:
            ctx.add("lo.bloom_inferred", "info",
                    "Mục 4.2 không ghi Bloom/CDIO. Bloom là đề xuất của hệ thống, "
                    "CDIO để trống; cả hai cần giảng viên xác nhận.")

        # 7. Cấu trúc tối thiểu.
        if not chapters:
            ctx.add("chapter.none_extracted", "error", "Không trích được chương nào.")
        if rows and not chapter_lo_links:
            ctx.add("link.none_extracted", "error",
                    "Đọc được hàng mục 6 nhưng không lấy được quan hệ chương–LO nào.")

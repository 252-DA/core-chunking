"""
Curriculum entities — mô hình hoá đề cương môn học (DCMH) theo đúng cấu trúc tài liệu.

Nguyên tắc:
  - Chương ↔ LO là **nhiều–nhiều**. Một LO được dạy qua nhiều chương; một chương
    có thể phục vụ nhiều LO. Mã LO (``L.O.2.2``) **không** mã hoá số chương.
  - Mục tiêu học phần (mục 4.1) và chuẩn đầu ra (mục 4.2) là hai thứ khác nhau
    dù dùng chung ký hiệu ``L.O.n``. Chỉ mục 4.2 định nghĩa LO.
  - LO song ngữ là **một** thực thể có hai bản diễn đạt, không phải hai LO.
  - Mọi thực thể/liên kết mang ``source`` (mục, trang, ô) và ``provenance`` để
    phân biệt dữ kiện trích từ tài liệu với suy luận của hệ thống.
"""
from dataclasses import dataclass, field
from typing import Literal

# extracted  — đọc trực tiếp từ tài liệu
# inferred   — hệ thống suy ra, chưa ai xác nhận
# confirmed  — giảng viên đã xác nhận
Provenance = Literal["extracted", "inferred", "confirmed"]

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class SourceRef:
    """Chỉ ra chỗ trong tài liệu gốc mà một dữ kiện được lấy ra."""
    section: str                      # "4.2", "5.3", "6"
    page: int | None = None           # số trang 1-based trong PDF nguồn
    locator: str | None = None        # "table 0 row 3 col 2" | "block 12"

    def __str__(self) -> str:  # pragma: no cover - chỉ để log/hiển thị
        parts = [f"mục {self.section}"]
        if self.page is not None:
            parts.append(f"trang {self.page}")
        if self.locator:
            parts.append(self.locator)
        return ", ".join(parts)


@dataclass(frozen=True)
class ExtractionIssue:
    """Một chỗ chưa chắc chắn cần giảng viên xem lại trước khi dữ liệu đi tiếp."""
    code: str                         # "lo.missing_english", "ref.unknown_lo", ...
    severity: Severity
    message: str
    source: SourceRef | None = None

    @property
    def blocking(self) -> bool:
        return self.severity == "error"


@dataclass(frozen=True)
class Course:
    course_id: str
    code: str
    title_vi: str
    title_en: str | None = None
    credits: int | None = None
    semester: str | None = None       # "HK261"
    syllabus_version: str | None = None   # "DCMH.CO3011.9.1"
    lo_year: str | None = None        # năm chuẩn đầu ra áp dụng, "2026"


@dataclass(frozen=True)
class Chapter:
    chapter_id: str
    code: str                         # "7" — số chương như tài liệu ghi
    title: str
    order_index: int = 0
    title_en: str | None = None
    source: SourceRef | None = None
    # Mục con của chương trong bảng mục 6 ("Min-hashing", "Locality sensitive
    # hashing") — tín hiệu để khớp nội dung tài liệu với chương.
    topics: tuple[str, ...] = ()


@dataclass(frozen=True)
class CourseGoal:
    """Mục 4.1 — mục tiêu học phần. Dùng chung ký hiệu L.O.n nhưng KHÔNG phải LO."""
    code: str                         # "L.O.1"
    statement_vi: str
    statement_en: str | None = None
    source: SourceRef | None = None


@dataclass(frozen=True)
class LearningOutcome:
    """Mục 4.2 — chuẩn đầu ra. Không gắn chương; quan hệ chương nằm ở ChapterLOLink."""
    lo_id: str                        # "CO3011:L.O.2.2"
    code: str                         # "L.O.2.2"
    parent_code: str | None           # "L.O.2"
    statement_vi: str
    statement_en: str | None = None
    # Bloom/CDIO không được ghi trong mục 4.2 của định dạng này. Để None thay vì
    # bịa; nếu có giá trị thì provenance nói rõ nó là đề xuất của hệ thống.
    bloom_level: str | None = None
    bloom_provenance: Provenance = "inferred"
    cdio_level: int | None = None
    cdio_provenance: Provenance = "inferred"
    source: SourceRef | None = None

    @property
    def level(self) -> int:
        """1 cho L.O.3, 2 cho L.O.3.2 — độ sâu trong cây LO."""
        return len(self.code.replace("L.O.", "").split("."))


@dataclass(frozen=True)
class Assessment:
    """Mục 5.2 — hoạt động đánh giá. Có cây cha–con giống LO (A.O.1 → A.O.1.1)."""
    assessment_id: str                # "CO3011:A.O.1.1"
    code: str                         # "A.O.1.1"
    name_vi: str
    parent_code: str | None = None    # "A.O.1"
    name_en: str | None = None
    activity_type: str | None = None  # "GPJ" | "EXM" — mã loại hoạt động trong tài liệu
    category: str = "quiz"            # quiz | midterm | final | project | group_quiz
    # Tỷ trọng lấy từ bảng tổ chức học phần (mục 1.1) và chỉ gán được khi khớp
    # chắc chắn theo loại hoạt động. Không suy từ 40/60 ra trọng số cạnh LO–AO.
    weight: float | None = None
    weight_provenance: Provenance = "inferred"
    source: SourceRef | None = None


@dataclass(frozen=True)
class SyllabusRow:
    """Một hàng của bảng mục 6 — một buổi học."""
    order_index: int                  # vị trí hàng trong bảng, 1-based
    session_no: int | None            # số ghi ở cột Buổi, None khi ô ghi "Chương n"
    chapter_code: str | None          # "7" khi ô ghi "Chương 7"; None với buổi ôn/báo cáo
    title_vi: str
    title_en: str | None = None
    lo_codes: tuple[str, ...] = ()
    assessment_codes: tuple[str, ...] = ()
    source: SourceRef | None = None
    topics: tuple[str, ...] = ()

    @property
    def is_chapter(self) -> bool:
        return self.chapter_code is not None


@dataclass(frozen=True)
class ChapterLOLink:
    """Chương ↔ LO, nhiều–nhiều, như bảng mục 6 ghi."""
    chapter_code: str
    lo_code: str
    provenance: Provenance = "extracted"
    source: SourceRef | None = None


@dataclass(frozen=True)
class LOAssessmentLink:
    """
    LO ↔ hoạt động đánh giá.

    ``row_order_index`` khác None nghĩa là cạnh này được tài liệu khẳng định
    trong ngữ cảnh một hàng cụ thể của mục 6 (ví dụ Chương 10 chỉ đánh giá
    ``L.O.3`` qua ``A.O.1``), chứ không phải phát biểu chung ở mục 5.3.
    """
    lo_code: str
    assessment_code: str
    row_order_index: int | None = None
    chapter_code: str | None = None
    provenance: Provenance = "extracted"
    source: SourceRef | None = None


@dataclass(frozen=True)
class Curriculum:
    course: Course
    chapters: tuple[Chapter, ...]
    learning_outcomes: tuple[LearningOutcome, ...]
    assessments: tuple[Assessment, ...]
    lo_assessment_links: tuple[LOAssessmentLink, ...] = ()
    goals: tuple[CourseGoal, ...] = ()
    sessions: tuple[SyllabusRow, ...] = ()
    chapter_lo_links: tuple[ChapterLOLink, ...] = ()
    issues: tuple[ExtractionIssue, ...] = ()
    source_document_id: str | None = None

    # ------------------------------------------------------------------
    # Tra cứu
    # ------------------------------------------------------------------

    @property
    def lo_by_code(self) -> dict[str, LearningOutcome]:
        return {lo.code: lo for lo in self.learning_outcomes}

    @property
    def assessment_by_code(self) -> dict[str, Assessment]:
        return {a.code: a for a in self.assessments}

    @property
    def chapter_by_code(self) -> dict[str, Chapter]:
        return {c.code: c for c in self.chapters}

    def los_for_chapter(self, chapter_code: str) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                link.lo_code
                for link in self.chapter_lo_links
                if link.chapter_code == chapter_code
            )
        )

    def chapters_for_lo(self, lo_code: str) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                link.chapter_code
                for link in self.chapter_lo_links
                if link.lo_code == lo_code
            )
        )

    # ------------------------------------------------------------------
    # Cổng duyệt — thay cho điểm confidence trung bình
    # ------------------------------------------------------------------

    @property
    def blocking_issues(self) -> tuple[ExtractionIssue, ...]:
        return tuple(i for i in self.issues if i.blocking)

    @property
    def needs_review(self) -> bool:
        return bool(self.issues)

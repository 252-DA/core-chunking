"""
Đánh giá kết quả parse PDF theo từng trang.

Trước đây cả file được kết luận bằng một câu hỏi duy nhất: "có lấy được text
nào không?". Một giáo trình 100 trang trong đó 10 trang là scan vẫn trả về
"parse thành công" và 10 trang đó mất im lặng.

Module này tách phần *đo và phân loại* ra khỏi parser: mỗi trang có status
riêng, status quyết định trang đó có cần backend thứ hai (OCR / layout) hay
không, và quyết định document được phép báo "đầy đủ" hay chỉ "một phần".

Trạng thái một trang đi qua ba trường:
  - ``status``     : kết luận của bước đo trên text layer (PyMuPDF)
  - ``attempts``   : các backend đã chạy trên trang đó
  - ``parsed_by``  : backend thực sự lấy được nội dung (None = chưa đọc được)

Nhờ đó phân biệt được ba tình huống rất khác nhau khi kết thúc:
  - trang chưa ai xử lý          → mất dữ liệu, phải báo lỗi
  - trang đã OCR nhưng không có text → đã xử lý, ghi nhận là no-text
  - trang có text nhưng layout phức tạp → có dữ liệu, thứ tự chưa chắc đúng
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from document_chunk.domain.exceptions import IncompletePageCoverageError

BBox = tuple[float, float, float, float]


class PageStatus(str, Enum):
    """Kết luận của bước đo text layer trên một trang."""

    TEXT = "text"                      # text layer dùng được, layout đơn giản
    COMPLEX_LAYOUT = "complex_layout"  # có text nhưng nhiều cột / có bảng
    NEEDS_OCR = "needs_ocr"            # không có text layer dùng được
    EMPTY = "empty"                    # trang trắng: không text, không ảnh, không vector


#: Trang mà text path của PyMuPDF không tự phục vụ được.
FALLBACK_STATUSES = frozenset({PageStatus.NEEDS_OCR, PageStatus.COMPLEX_LAYOUT})

#: Trang thiếu hẳn nội dung text — khác với trang chỉ sai thứ tự đọc.
MISSING_TEXT_STATUSES = frozenset({PageStatus.NEEDS_OCR})


@dataclass(frozen=True)
class PageAssessment:
    """Số đo và kết luận cho một trang."""

    page_number: int
    status: PageStatus
    char_count: int
    line_count: int
    image_area_ratio: float
    multi_column: bool
    table_count: int
    reasons: tuple[str, ...] = ()

    @property
    def needs_fallback(self) -> bool:
        return self.status in FALLBACK_STATUSES

    def to_dict(self, *, backend: str) -> dict:
        """
        Dạng dict đưa vào ``ParsedDocument.metadata['page_report']``.

        ``parsed_by`` = backend đang giữ nội dung của trang. Trang
        ``needs_ocr`` luôn để None kể cả khi có vài ký tự lọt ra từ watermark
        hay số trang stamp — text layer đó không dùng được.
        """
        held = self.char_count > 0 and self.status is not PageStatus.NEEDS_OCR
        return {
            "page": self.page_number,
            "status": self.status.value,
            "chars": self.char_count,
            "lines": self.line_count,
            "image_area_ratio": round(self.image_area_ratio, 3),
            "multi_column": self.multi_column,
            "tables": self.table_count,
            "reasons": list(self.reasons),
            "attempts": [backend],
            "parsed_by": backend if held else None,
            "layout_aware": False,
        }


# ---------------------------------------------------------------------------
# Phân loại trang
# ---------------------------------------------------------------------------

def classify_page(
    *,
    page_number: int,
    char_count: int,
    line_count: int,
    image_area_ratio: float,
    drawing_count: int,
    multi_column: bool,
    table_count: int,
    min_page_chars: int,
    scan_image_coverage: float,
) -> PageAssessment:
    """
    Phân loại một trang từ số đo thô.

    Thứ tự kiểm tra quan trọng: trang scan có thể vẫn có vài ký tự (số trang
    được stamp, watermark) nên phải xét độ phủ ảnh trước khi kết luận "trang
    ít text nhưng đọc được".
    """
    reasons: list[str] = []
    status: PageStatus

    if char_count >= min_page_chars:
        if multi_column:
            reasons.append("multi_column")
        if table_count:
            reasons.append("table_detected")
        status = PageStatus.COMPLEX_LAYOUT if reasons else PageStatus.TEXT
    elif image_area_ratio >= scan_image_coverage:
        status = PageStatus.NEEDS_OCR
        reasons.append("image_page")
    elif image_area_ratio > 0 or drawing_count > 0:
        status = PageStatus.NEEDS_OCR
        reasons.append("no_text_layer")
    elif char_count > 0:
        status = PageStatus.TEXT
        reasons.append("sparse_text")
    else:
        status = PageStatus.EMPTY

    return PageAssessment(
        page_number=page_number,
        status=status,
        char_count=char_count,
        line_count=line_count,
        image_area_ratio=min(image_area_ratio, 1.0),
        multi_column=multi_column,
        table_count=table_count,
        reasons=tuple(reasons),
    )


def has_column_gutter(
    line_boxes: Sequence[BBox],
    page_width: float,
    *,
    band: tuple[float, float] = (0.2, 0.8),
    samples: int = 33,
    min_side_ratio: float = 0.25,
    min_side_lines: int = 3,
    crossing_ratio: float = 0.05,
) -> bool:
    """
    Tìm rãnh dọc chia trang thành hai khối text song song.

    Quét các vị trí x trong dải giữa trang; một vị trí là rãnh khi hầu như
    không có dòng nào cắt qua, hai bên đều đủ nhiều dòng, và hai bên phủ cùng
    một khoảng dọc (tránh nhận nhầm header trái + footer phải là hai cột).

    Chỉ trả lời "có nhiều hơn một cột hay không" — việc dựng lại thứ tự đọc là
    việc của layout backend, không phải của heuristic này.
    """
    boxes = [b for b in line_boxes if b[2] > b[0]]
    if page_width <= 0 or len(boxes) < 2 * min_side_lines:
        return False

    tolerance = max(1.0, page_width * 0.004)
    allowed_crossings = int(len(boxes) * crossing_ratio)
    min_side = max(min_side_lines, int(len(boxes) * min_side_ratio))
    low, high = band[0] * page_width, band[1] * page_width
    step = (high - low) / (samples - 1) if samples > 1 else 0.0

    for index in range(samples):
        x = low + step * index
        left: list[BBox] = []
        right: list[BBox] = []
        crossings = 0
        for box in boxes:
            if box[2] <= x + tolerance:
                left.append(box)
            elif box[0] >= x - tolerance:
                right.append(box)
            else:
                crossings += 1
                if crossings > allowed_crossings:
                    break
        if crossings > allowed_crossings:
            continue
        if len(left) < min_side or len(right) < min_side:
            continue
        if _vertical_overlap(left, right) >= 0.5:
            return True
    return False


def _vertical_overlap(left: Sequence[BBox], right: Sequence[BBox]) -> float:
    """Tỉ lệ chồng nhau theo trục dọc giữa hai khối, so với khối ngắn hơn."""
    top = max(min(b[1] for b in left), min(b[1] for b in right))
    bottom = min(max(b[3] for b in left), max(b[3] for b in right))
    overlap = bottom - top
    if overlap <= 0:
        return 0.0
    spans = [
        max(b[3] for b in group) - min(b[1] for b in group)
        for group in (left, right)
    ]
    shortest = min(spans)
    return overlap / shortest if shortest > 0 else 0.0


# ---------------------------------------------------------------------------
# Báo cáo mức document
# ---------------------------------------------------------------------------

def build_report(
    assessments: Iterable[PageAssessment], *, backend: str
) -> list[dict]:
    return [a.to_dict(backend=backend) for a in assessments]


def pages_needing_fallback(report: Sequence[dict]) -> list[int]:
    """
    Trang cần backend OCR/layout và chưa backend nào thử.

    Gồm cả trang ``complex_layout`` đã có text: text có nhưng thứ tự đọc và
    quan hệ hàng–cột chưa đảm bảo, nên vẫn cần đọc lại bằng layout backend.
    """
    fallback = {s.value for s in FALLBACK_STATUSES}
    return [
        entry["page"]
        for entry in report
        if entry.get("status") in fallback and len(entry.get("attempts", [])) <= 1
    ]


def unprocessed_pages(report: Sequence[dict]) -> list[int]:
    """
    Trang thiếu text mà **chưa có backend nào thử đọc**.

    Đây là mất dữ liệu thật sự: khác với trang đã OCR nhưng không tìm thấy
    text (ảnh không chứa chữ) — trường hợp đó đã được xử lý và ghi nhận.
    """
    missing = {s.value for s in MISSING_TEXT_STATUSES}
    return [
        entry["page"]
        for entry in report
        if entry.get("parsed_by") is None
        and entry.get("status") in missing
        and len(entry.get("attempts", [])) <= 1
    ]


def no_text_pages(report: Sequence[dict]) -> list[int]:
    """Trang đã chạy fallback nhưng vẫn không ra text — ảnh/diagram không chữ."""
    missing = {s.value for s in MISSING_TEXT_STATUSES}
    return [
        entry["page"]
        for entry in report
        if entry.get("parsed_by") is None
        and entry.get("status") in missing
        and len(entry.get("attempts", [])) > 1
    ]


def degraded_pages(report: Sequence[dict]) -> list[int]:
    """
    Trang có text nhưng chưa backend layout nào đọc lại.

    Không phải mất dữ liệu — là dữ liệu chưa chắc đúng thứ tự. Ghi nhận và cảnh
    báo, không chặn pipeline.
    """
    return [
        entry["page"]
        for entry in report
        if entry.get("status") == PageStatus.COMPLEX_LAYOUT.value
        and not entry.get("layout_aware")
    ]


def mark_failure(
    report: Sequence[dict], pages: Iterable[int], backend: str
) -> None:
    """
    Backend lỗi trên các trang này — KHÔNG tính là đã xử lý.

    Backend chạy xong mà không ra text là một kết luận; backend chết giữa đường
    thì trang vẫn chưa ai đọc và phải báo lỗi.
    """
    wanted = set(pages)
    reason = f"{backend}_failed"
    for entry in report:
        if entry["page"] in wanted and reason not in entry.get("reasons", []):
            entry.setdefault("reasons", []).append(reason)


def mark_attempt(report: Sequence[dict], pages: Iterable[int], backend: str) -> None:
    """Ghi nhận backend đã chạy xong trên các trang này (kể cả khi không ra text)."""
    wanted = set(pages)
    for entry in report:
        if entry["page"] in wanted and backend not in entry.get("attempts", []):
            entry.setdefault("attempts", []).append(backend)


def mark_parsed(
    report: Sequence[dict],
    pages: Iterable[int],
    backend: str,
    *,
    layout_aware: bool = False,
) -> None:
    """
    Ghi nhận backend đã lấy được nội dung cho các trang này.

    ``layout_aware=True`` khi backend hiểu cấu trúc trang (cột, bảng) — chỉ khi
    đó trang ``complex_layout`` mới hết bị tính là degraded.
    """
    wanted = set(pages)
    for entry in report:
        if entry["page"] in wanted:
            entry["parsed_by"] = backend
            if layout_aware:
                entry["layout_aware"] = True


def compact_ranges(pages: Sequence[int]) -> list[list[int]]:
    """[1,2,3,7,8] → [[1,3],[7,8]] — để metadata không phình theo số trang."""
    ranges: list[list[int]] = []
    for page in sorted(set(pages)):
        if ranges and page == ranges[-1][1] + 1:
            ranges[-1][1] = page
        else:
            ranges.append([page, page])
    return ranges


def contiguous_ranges(pages: Sequence[int], max_ranges: int) -> list[tuple[int, int]]:
    """
    Gom trang thành các khoảng liên tiếp, tối đa ``max_ranges`` khoảng.

    Backend layout chỉ nhận một khoảng trang mỗi lần chạy. Khi số khoảng vượt
    hạn mức, nối các khoảng gần nhau nhất lại — thà OCR thêm vài trang còn hơn
    gọi backend hàng chục lần.
    """
    ranges = [(r[0], r[1]) for r in compact_ranges(pages)]
    if max_ranges <= 0:
        return ranges
    while len(ranges) > max_ranges:
        gaps = [ranges[i + 1][0] - ranges[i][1] for i in range(len(ranges) - 1)]
        index = gaps.index(min(gaps))
        ranges[index : index + 2] = [(ranges[index][0], ranges[index + 1][1])]
    return ranges


def coverage(
    report: Sequence[dict],
    *,
    pages_total: int,
    pages_examined: int,
) -> dict:
    """Tổng hợp mức document — luôn kèm danh sách trang chưa đọc được."""
    by_status: dict[str, int] = {}
    backends: dict[str, int] = {}
    for entry in report:
        by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
        parsed_by = entry.get("parsed_by")
        if parsed_by:
            backends[parsed_by] = backends.get(parsed_by, 0) + 1

    unprocessed = unprocessed_pages(report)
    no_text = no_text_pages(report)
    degraded = degraded_pages(report)
    resolved = sum(
        1
        for entry in report
        if entry.get("parsed_by") or entry["status"] == PageStatus.EMPTY.value
    )
    skipped = [p for p in range(pages_examined + 1, pages_total + 1)]

    complete = not unprocessed and not no_text and not degraded and not skipped
    return {
        "status": "complete" if complete else "partial",
        "pages_total": pages_total,
        "pages_examined": pages_examined,
        "pages_resolved": resolved,
        "unprocessed_pages": unprocessed,
        "no_text_pages": no_text,
        "degraded_pages": degraded,
        "pages_skipped_by_limit": compact_ranges(skipped),
        "by_status": by_status,
        "by_backend": backends,
    }


def format_pages(pages: Sequence[int]) -> str:
    """[3,4,5,12] → '3-5, 12' — dùng trong log và message lỗi."""
    parts = [
        str(start) if start == end else f"{start}-{end}"
        for start, end in compact_ranges(pages)
    ]
    return ", ".join(parts)


def missing_text_error(
    cov: dict, *, file_name: str, parsed: object | None = None
) -> IncompletePageCoverageError | None:
    """
    Lỗi cho phần chưa đọc được — hoặc None nếu mọi trang đã được xử lý.

    Chỉ tính các trang chưa backend nào thử đọc. Trang đã OCR mà không ra text
    (ảnh không chứa chữ) đã được xử lý và ghi nhận ở ``no_text_pages``.
    """
    pages = list(cov.get("unprocessed_pages") or [])
    if not pages:
        return None
    examined = cov.get("pages_examined", 0)
    return IncompletePageCoverageError(
        f"'{file_name}': {len(pages)}/{examined} trang chưa đọc được nội dung "
        f"(trang {format_pages(pages)}). Các trang còn lại có text không đủ để "
        f"kết luận parse thành công.",
        pages=pages,
        page_count=examined,
        parsed=parsed,
    )

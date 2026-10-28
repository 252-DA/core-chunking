"""
Chế độ PDF slide: mỗi trang là một slide, tiêu đề là khối chữ lớn nhất trang.

PDF xuất từ PowerPoint không có heading thật. Đoán heading theo cỡ chữ trên cả
file biến mọi dòng in to hoặc in đậm thành một mục: Ch2_LSH 101 trang ra 240
"heading", ghi chú trong slide thành mục con và các slide sau bị lồng sai cấp.
Ở chế độ này parser trả về một Section SLIDE mỗi trang, cùng dạng với
PptxParser (``heading`` = tiêu đề, ``metadata['blocks']`` = các khối chữ), nên
chunker xử lý PDF slide giống hệt PPTX.

Nhận diện (``pdf_slide_mode="auto"``): phần lớn trang khổ ngang và mật độ chữ
thấp. Slide 16:9, 4:3 và A4 ngang đều qua; bài báo khổ dọc ~6.500 ký tự/trang
thì không.
"""
import math
import re
import statistics
from collections import defaultdict

from document_chunk.domain.entities.document import ElementType, Section

#: Trang được coi là khổ ngang khi rộng ≥ chừng này lần cao.
LANDSCAPE_RATIO = 1.2
#: Tỉ lệ trang khổ ngang tối thiểu để cả file là slide.
MIN_LANDSCAPE_SHARE = 0.8
#: Slide chữ dày nhất trong tài liệu môn học ~1.000 ký tự; bài báo ~6.000.
MAX_MEDIAN_PAGE_CHARS = 1500
#: Tiêu đề phải lớn hơn cỡ chữ thân của cả bộ slide ít nhất chừng này lần.
TITLE_SIZE_RATIO = 1.25
#: Tiêu đề nằm trong phần trên của trang (slide ngăn chương đặt tiêu đề giữa trang).
TITLE_MAX_TOP = 0.7
TITLE_MAX_WORDS = 20
#: Trang chỉ có một dòng ngắn: dòng đó là tiêu đề dù không in to.
LONE_TITLE_MAX_WORDS = 12

# "·": font Base14 trả "•" về thành dấu chấm giữa khi trích chữ.
_GLYPHS = "•·▪◦‣➢➤►▶■□●○§Ø✓❖➔–\\-*"
_BULLET = re.compile(rf"^\s*(?:[{_GLYPHS}]|o(?=\s+\S))\s*")
_BULLET_ONLY = re.compile(rf"^\s*(?:[{_GLYPHS}]|o)\s*$")
_IMAGE_PLACEHOLDER = re.compile(r"^\s*\[(?:image|hình)[^\]]*\]\s*$", re.I)
_WORD = re.compile(r"[^\W\d_]{2,}")


def looks_like_slides(pages: dict[int, dict], mode: str) -> bool:
    """``pages``: số trang → {"width", "height", "lines"} như PdfParser đọc được."""
    if mode == "always":
        return True
    if mode == "never" or not pages:
        return False
    landscape = sum(p["width"] >= p["height"] * LANDSCAPE_RATIO for p in pages.values())
    if landscape < len(pages) * MIN_LANDSCAPE_SHARE:
        return False
    chars = [sum(len(line["text"]) for line in p["lines"]) for p in pages.values()]
    return statistics.median(chars) <= MAX_MEDIAN_PAGE_CHARS


def build_slide_sections(pages: dict[int, dict], parser_name: str) -> list[Section]:
    """Một Section SLIDE cho mỗi trang có chữ, theo thứ tự trang."""
    body_size = _body_size(pages)
    footers = _repeated_small_lines(pages, body_size)
    sections = []
    for number in sorted(pages):
        page = pages[number]
        lines = [line for line in page["lines"] if _footer_key(line["text"]) not in footers]
        title_lines = _title_lines(lines, page["height"], body_size)
        title = _join([line["text"] for line in title_lines]) or None
        rest = [line for line in lines if line not in title_lines]
        section = slide_section(
            number,
            title,
            _blocks(rest),
            source={"parser": parser_name, "page": number, "layout": "slide"},
        )
        if section is not None:
            sections.append(section)
    return sections


def slide_section(
    number: int, title: str | None, blocks: list[dict], *, source: dict
) -> Section | None:
    """Cùng dạng Section mà PptxParser tạo cho một slide; None khi trang trống."""
    content = "\n\n".join(block["text"] for block in blocks)
    if not title and not content:
        return None
    return Section(
        content=content or title or "",
        element_type=ElementType.SLIDE,
        heading=title,
        heading_level=1 if title else 0,
        page_number=number,
        metadata={"slide_number": number, "blocks": blocks, "source": source},
    )


def fold_into_slide(
    number: int, replacement: list[Section], text_slide: Section | None
) -> Section | None:
    """
    Gộp các section Docling đọc lại cho một trang thành một slide.

    Docling đọc lại trang cần OCR hoặc layout, nhưng không biết khái niệm slide.
    Tiêu đề lấy từ text path nếu đã có (cỡ chữ đáng tin hơn heading Docling đoán),
    nếu không thì lấy heading đầu tiên Docling thấy.
    """
    title = text_slide.heading if text_slide is not None else None
    if not title:
        title = next(
            (
                _join([s.content])
                for s in replacement
                if s.element_type == ElementType.HEADING
                and len(s.content.split()) <= TITLE_MAX_WORDS
            ),
            None,
        )
    blocks = []
    for sec in replacement:
        text = sec.content.strip()
        if not text or _IMAGE_PLACEHOLDER.match(text) or (title and _key(text) == _key(title)):
            continue
        kind = {
            ElementType.TABLE: "table",
            ElementType.LIST: "list",
            ElementType.CODE: "code",
        }.get(sec.element_type, "text")
        blocks.append({"kind": kind, "text": text})
    if not blocks and text_slide is not None:
        blocks = list(text_slide.metadata.get("blocks", []))
    source = {"parser": "docling", "page": number, "layout": "slide"}
    return slide_section(number, title, blocks, source=source)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _key(text: str) -> str:
    return re.sub(r"\d+", "#", " ".join(text.split()).casefold())


def _footer_key(text: str) -> str:
    """``HPC Lab - CSE - HCMUT, 2024`` và ``HPC Lab-CSE-HCMUT`` là cùng một nhãn."""
    return re.sub(r"[\W\d_]+", "", text.casefold())


def _body_size(pages: dict[int, dict]) -> float:
    """Cỡ chữ thân của cả bộ: trung vị theo số ký tự, không theo số dòng."""
    sized = sorted(
        (line["size"], len(line["text"])) for p in pages.values() for line in p["lines"]
    )
    total = sum(n for _, n in sized)
    if not total:
        return 12.0
    seen = 0
    for size, n in sized:
        seen += n
        if seen * 2 >= total:
            return size
    return sized[-1][0]


def _repeated_small_lines(pages: dict[int, dict], body_size: float) -> set[str]:
    """
    Chân trang nằm ngoài dải lề (``HPC Lab-CSE-HCMUT`` ở y=0,03) lọt qua bước bỏ
    header/footer của PdfParser. Dòng nhỏ hơn chữ thân lặp lại trên nhiều trang
    là nhãn của template, không phải nội dung.
    """
    occurrences: dict[str, set[int]] = defaultdict(set)
    for number, page in pages.items():
        for line in page["lines"]:
            if line["size"] < body_size and (key := _footer_key(line["text"])):
                occurrences[key].add(number)
    threshold = max(3, math.ceil(len(pages) * 0.3))
    return {key for key, seen in occurrences.items() if len(seen) >= threshold}


def _title_lines(lines: list[dict], height: float, body_size: float) -> list[dict]:
    # Số thứ tự bước in rất to ("1", "2", "3" cỡ 48 trên slide tiêu đề cỡ 36)
    # không phải tiêu đề: tiêu đề phải có chữ.
    lines = [line for line in lines if _WORD.search(line["text"])]
    if not lines:
        return []
    top = max(line["size"] for line in lines)
    if top < body_size * TITLE_SIZE_RATIO:
        only = lines[0]["text"] if len(lines) == 1 else ""
        if not only or len(only.split()) > LONE_TITLE_MAX_WORDS or _BULLET.match(only):
            # Không dòng nào nổi bật: slide tiếp nối của slide trước.
            return []
    candidates = [
        line
        for line in lines
        if line["size"] >= top - 0.5 and line["bbox"][1] < height * TITLE_MAX_TOP
    ]
    if not candidates:
        return []
    first = min(candidates, key=lambda line: (line["bbox"][1], line["bbox"][0]))
    title = [line for line in candidates if line["block_index"] == first["block_index"]]
    # Tiêu đề hai dòng đôi khi nằm ở hai khối liền nhau (Ch6 trang 1).
    for line in sorted(candidates, key=lambda line: line["bbox"][1]):
        last = title[-1]
        gap = line["bbox"][1] - last["bbox"][3]
        if line not in title and -0.5 * last["size"] <= gap <= 0.8 * last["size"]:
            title.append(line)
    if sum(len(line["text"].split()) for line in title) > TITLE_MAX_WORDS:
        return []
    return title


def _blocks(lines: list[dict]) -> list[dict]:
    """Gom dòng theo khối PyMuPDF, xếp từ trên xuống rồi trái sang phải."""
    grouped: dict[int, list[dict]] = {}
    for line in lines:
        grouped.setdefault(line["block_index"], []).append(line)
    ordered = sorted(
        grouped.values(), key=lambda group: (group[0]["bbox"][1], group[0]["bbox"][0])
    )
    blocks = []
    for group in ordered:
        for text in _items(group):
            listed = bool(_BULLET.match(text))
            if listed:
                text = "• " + _BULLET.sub("", text, count=1)
            blocks.append({"kind": "list" if listed else "text", "text": text})
    return blocks


def _items(lines: list[dict]) -> list[str]:
    """Một khối có thể chứa nhiều gạch đầu dòng; mỗi gạch là một mục."""
    items: list[list[str]] = []
    pending_glyph = False
    for line in lines:
        text = line["text"].strip()
        if _BULLET_ONLY.match(text):
            # Wingdings tách ký hiệu "§" thành dòng riêng trước nội dung.
            pending_glyph = True
            continue
        starts_item = pending_glyph or bool(_BULLET.match(text))
        if pending_glyph:
            text = "• " + text
            pending_glyph = False
        if starts_item or not items:
            items.append([text])
        else:
            items[-1].append(text)
    return [joined for parts in items if (joined := _join(parts))]


def _join(parts: list[str]) -> str:
    """Nối các dòng; nối lại chữ bị ngắt bằng gạch nối ở cuối dòng."""
    text = ""
    for part in (p.strip() for p in parts):
        if not part:
            continue
        if text.endswith("-") and part[:1].islower():
            text = text[:-1] + part
        else:
            text = f"{text} {part}" if text else part
    return text

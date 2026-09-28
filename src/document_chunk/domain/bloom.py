"""
Bloom — một chỗ duy nhất quy đổi giữa mức số (1–6) và tên mức.

Lý do tồn tại: `learning_outcomes.bloom_level` là INT trong Postgres, prompt lại
cần tên mức ("analyze"), và trước đây mỗi nơi tự quy đổi một kiểu. Hệ quả đã
quan sát được: LO mức 3 đọc lên thành int 3, bị `str()` thành "3", không khớp
bảng tên nên rơi về mặc định 2 — **mọi câu hỏi sinh theo LO đều lưu mức 2**.

Ở đây chấp nhận cả ba dạng đầu vào (int, "3", "analyze") và không có giá trị
mặc định âm thầm: không biết thì trả None để chỗ gọi tự quyết định.
"""
from __future__ import annotations

BLOOM_NAMES: tuple[str, ...] = (
    "remember",
    "understand",
    "apply",
    "analyze",
    "evaluate",
    "create",
)

_NAME_TO_LEVEL: dict[str, int] = {name: i + 1 for i, name in enumerate(BLOOM_NAMES)}

# Tên tiếng Việt hay gặp trong đề cương CDIO.
_ALIASES: dict[str, str] = {
    "nhớ": "remember",
    "hiểu": "understand",
    "áp dụng": "apply",
    "vận dụng": "apply",
    "phân tích": "analyze",
    "đánh giá": "evaluate",
    "sáng tạo": "create",
    "tổng hợp": "create",
    "knowledge": "remember",
    "comprehension": "understand",
    "application": "apply",
    "analysis": "analyze",
    "evaluation": "evaluate",
    "synthesis": "create",
}


def bloom_to_level(value: str | int | None) -> int | None:
    """int 1–6, hoặc None khi không nhận ra. Không mặc định về 2."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 6 else None

    text = str(value).strip().lower()
    if not text:
        return None
    # "3" đọc lên từ cột INT rồi bị str() ở đâu đó trên đường đi.
    if text.isdigit():
        n = int(text)
        return n if 1 <= n <= 6 else None
    if text in _NAME_TO_LEVEL:
        return _NAME_TO_LEVEL[text]
    return _NAME_TO_LEVEL.get(_ALIASES.get(text, ""))


def bloom_to_name(value: str | int | None) -> str | None:
    """Tên mức dùng trong prompt, hoặc None khi không nhận ra."""
    level = bloom_to_level(value)
    return BLOOM_NAMES[level - 1] if level else None


__all__ = ["BLOOM_NAMES", "bloom_to_level", "bloom_to_name"]

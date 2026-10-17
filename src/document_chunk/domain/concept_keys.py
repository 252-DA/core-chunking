"""
Chuẩn hóa khóa khái niệm và kiểm tra trích dẫn của đồ thị quan hệ.

Hai bên dùng chung hàm này: worker ghi ``source_key``/``target_key`` khi trích
quan hệ, còn công cụ MCP tra theo cùng một khóa. Nếu hai nơi chuẩn hóa khác
nhau thì khái niệm vừa trích sẽ không bao giờ tra ra.
"""
from __future__ import annotations

import re
import unicodedata

# Loại quan hệ được phép. Giữ ít và rõ nghĩa để mô hình chọn nhất quán và để
# người đọc hiểu một cạnh mà không cần thêm chú thích.
RELATION_TYPES = (
    "cause",         # nguồn gây ra / dẫn tới đích
    "part_of",       # nguồn là một phần, thành phần của đích
    "contrast",      # nguồn đối lập, khác với đích
    "prerequisite",  # cần hiểu nguồn trước khi hiểu đích
    "example_of",    # nguồn là ví dụ của đích
    "defines",       # nguồn được định nghĩa là đích (đồng nhất, mô tả)
    "related",       # có liên hệ nhưng không thuộc loại trên
)

MAX_LABEL_CHARS = 120

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)


def fold_text(text: str) -> str:
    """NFC → bỏ dấu → hạ chữ thường → gộp khoảng trắng. Dùng để so khớp mềm."""
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    stripped = stripped.replace("đ", "d").replace("Đ", "d")
    return _WS.sub(" ", stripped.casefold()).strip()


def concept_key(label: str) -> str:
    """Khóa khái niệm: như ``fold_text`` và bỏ dấu câu. Rỗng nếu không còn gì."""
    return _WS.sub(" ", _PUNCT.sub(" ", fold_text(label))).strip()[:MAX_LABEL_CHARS]


def normalize_for_quote(text: str) -> str:
    """Chuẩn hóa để so một trích dẫn với đoạn nguồn: NFC, gộp khoảng trắng, hạ chữ thường."""
    return _WS.sub(" ", unicodedata.normalize("NFC", text or "")).strip().casefold()


def quote_is_verbatim(quote: str, source_text: str, *, min_chars: int = 12) -> bool:
    """Trích dẫn có phải chuỗi con của đoạn nguồn (sau chuẩn hóa) không."""
    needle = normalize_for_quote(quote)
    if len(needle) < min_chars:
        return False
    return needle in normalize_for_quote(source_text)


__all__ = [
    "MAX_LABEL_CHARS",
    "RELATION_TYPES",
    "concept_key",
    "fold_text",
    "normalize_for_quote",
    "quote_is_verbatim",
]

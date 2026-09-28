"""
Xáo phương án trắc nghiệm một cách tất định, với vị trí đáp án đúng cân bằng.

Hai vấn đề tách biệt:

1. **Không xáo gì cả.** Model có xu hướng đặt đáp án đúng ở một vị trí cố định,
   và core-api phục vụ các phương án đúng thứ tự đã lưu, nên vị trí trở thành
   manh mối. Đo được bằng phân bố `correct_index` trong `quiz_items`.
2. **Xáo ngẫu nhiên thuần** thì một mẻ 4 câu vẫn có thể dồn hết đáp án đúng vào
   cùng một vị trí. Ở đây phân bổ vị trí đích theo vòng (0,1,2,3,0,…) rồi hoán
   vị để đưa đáp án đúng về đúng chỗ, nên phân bố phẳng theo thiết kế.

Tất định theo ``seed``: chạy lại cùng một mẻ cho ra cùng một bố cục, nên retry
không sinh ra phiên bản khác của cùng câu hỏi.
"""
from __future__ import annotations

import hashlib
import random


def _rng(seed: str) -> random.Random:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def shuffle_choices(
    choices: list[str],
    correct_index: int,
    *,
    seed: str,
    target_index: int,
) -> tuple[list[str], int]:
    """
    Trả về (phương án đã xáo, vị trí mới của đáp án đúng).

    Đáp án đúng được đặt vào ``target_index``; các phương án còn lại xáo theo
    ``seed``. ``correct_index`` ngoài khoảng thì giữ nguyên đầu vào — dữ liệu
    hỏng phải đi tiếp để validator bắt, không im lặng sửa thành câu khác.
    """
    if not choices or not (0 <= correct_index < len(choices)):
        return list(choices), correct_index

    target = target_index % len(choices)
    correct = choices[correct_index]
    others = [c for i, c in enumerate(choices) if i != correct_index]
    _rng(seed).shuffle(others)

    result = others[:target] + [correct] + others[target:]
    return result, target


def balanced_positions(count: int, *, options: int = 4, seed: str = "") -> list[int]:
    """
    ``count`` vị trí đích với số lần xuất hiện chênh nhau nhiều nhất là 1.

    Điểm bắt đầu của vòng phụ thuộc ``seed`` để hai mẻ liên tiếp không cùng bắt
    đầu ở vị trí 0.
    """
    if count <= 0 or options <= 0:
        return []
    start = _rng(seed).randrange(options) if seed else 0
    cycle = list(range(options))
    _rng(seed + ":order").shuffle(cycle)
    return [cycle[(start + i) % options] for i in range(count)]


__all__ = ["balanced_positions", "shuffle_choices"]

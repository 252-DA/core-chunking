"""IChapterMatcher — xác định tài liệu (hoặc một phần tài liệu) thuộc chương nào của đề cương."""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from document_chunk.shared.result import Result


@dataclass(frozen=True)
class ChapterProfile:
    code: str
    # Tên chương (VI/EN) và mục con trong bảng mục 6 — thứ để so với nội dung.
    text: str


@dataclass(frozen=True)
class ChapterMatch:
    """Kết quả cho một đoạn văn bản; chapter_code None khi không đủ chắc."""
    chapter_code: str | None
    score: float
    margin: float
    best_code: str | None = None
    runner_up_code: str | None = None


class IChapterMatcher(ABC):
    @abstractmethod
    def match(
        self,
        texts: list[str],
        chapters: list[ChapterProfile],
        *,
        strict: bool = False,
    ) -> Result[list[ChapterMatch], Exception]:
        """Một ChapterMatch cho mỗi text, cùng thứ tự.

        ``strict`` nâng ngưỡng — dùng cho từng phần của tài liệu tham khảo, nơi
        một đoạn ngắn dễ khớp nhầm.
        """
        ...

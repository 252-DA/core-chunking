from abc import ABC, abstractmethod
from pathlib import Path


class IPreprocessor(ABC):
    """
    Port: xử lý file trước khi parse.
    Ví dụ: OCR scanned PDF, extract images, detect language.
    Nhận Path vào, trả Path ra (có thể là file mới sau xử lý).
    """

    @abstractmethod
    def process(self, path: Path) -> Path:
        """
        Preprocess file.
        Trả về path của file đã xử lý (có thể là file tạm mới).
        Raise PreprocessError nếu thất bại.
        """
        ...

    @abstractmethod
    def should_apply(self, path: Path) -> bool:
        """
        Kiểm tra xem preprocessor này có cần áp dụng cho file không.
        Ví dụ: OCR chỉ cần apply nếu PDF là scanned.
        """
        ...

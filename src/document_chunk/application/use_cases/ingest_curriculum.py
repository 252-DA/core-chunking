"""
IngestCurriculumUseCase — parse đề cương (DCMH) → Curriculum. KHÔNG ghi gì.

Từ 2026-11-07 đề cương là dữ liệu nghiệp vụ của core-api: worker parse rồi gửi
bản chuẩn hoá về qua hàng đợi ``worker_results`` (CURRICULUM_IMPORT_RESULT),
core-api lưu lại cho giảng viên duyệt và tự ghi các bảng đề cương khi áp dụng.
Use case này vì vậy không còn phụ thuộc metadata store hay graph store. Xem
docs/ke-hoach-ranh-gioi-du-lieu.md.
"""
from dataclasses import dataclass
from pathlib import Path

from document_chunk.domain.entities.curriculum import Curriculum
from document_chunk.domain.exceptions import ProcessingError
from document_chunk.domain.ports.curriculum_extractor import ICurriculumExtractor
from document_chunk.domain.ports.parser import IParser
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)


@dataclass
class IngestCurriculumRequest:
    file_path: Path
    file_name: str
    course_id: str


class IngestCurriculumUseCase:
    def __init__(
        self,
        parsers: list[IParser],
        curriculum_extractor: ICurriculumExtractor,
    ) -> None:
        self._parsers = parsers
        self._extractor = curriculum_extractor

    def extract(self, request: IngestCurriculumRequest) -> Result[Curriculum, Exception]:
        """Parse + trích xuất, kể cả khi còn lỗi chặn — bản xem trước cần thấy cả lỗi."""
        parser = self._resolve_parser(request.file_name)
        if parser is None:
            return Err(ProcessingError(f"No parser for file: {request.file_name}"))

        parse_result = parser.parse(request.file_path)
        if parse_result.is_err():
            return Err(parse_result.error)

        return self._extractor.extract(parse_result.unwrap(), course_id_hint=request.course_id)

    def execute(self, request: IngestCurriculumRequest) -> Result[Curriculum, Exception]:
        """Như ``extract`` nhưng từ chối đề cương còn lỗi chặn (cổng duyệt cấu trúc)."""
        extract_result = self.extract(request)
        if extract_result.is_err():
            return Err(extract_result.error)

        curriculum: Curriculum = extract_result.unwrap()
        blocking = curriculum.blocking_issues
        if blocking:
            logger.warning(
                "ingest_curriculum.blocked",
                course_id=curriculum.course.course_id,
                errors=[i.code for i in blocking],
            )
            return Err(ProcessingError(
                "Đề cương chưa đạt kiểm tra cấu trúc: "
                + "; ".join(f"{i.code}: {i.message}" for i in blocking[:5])
            ))
        return Ok(curriculum)

    def _resolve_parser(self, file_name: str) -> IParser | None:
        suffix = Path(file_name).suffix.lower().lstrip(".")
        for parser in self._parsers:
            if suffix in (s.lstrip(".").lower() for s in getattr(parser, "supported_extensions", [])):
                return parser
            if hasattr(parser, "supports") and parser.supports(suffix):
                return parser
        # fallback: try each parser's doc_type
        for parser in self._parsers:
            doc_type = getattr(parser, "_doc_type", None) or getattr(parser, "doc_type", None)
            if doc_type and doc_type.value == suffix:
                return parser
        if self._parsers:
            return self._parsers[0]
        return None

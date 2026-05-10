from abc import ABC, abstractmethod

from src.domain.entities.curriculum import Curriculum
from src.domain.entities.document import ParsedDocument
from src.shared.result import Result


class ICurriculumExtractor(ABC):
    @abstractmethod
    def extract(
        self,
        parsed_doc: ParsedDocument,
        course_id_hint: str | None = None,
    ) -> Result[Curriculum, Exception]:
        ...

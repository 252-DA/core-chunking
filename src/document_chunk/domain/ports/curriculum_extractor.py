from abc import ABC, abstractmethod

from document_chunk.domain.entities.curriculum import Curriculum
from document_chunk.domain.entities.document import ParsedDocument
from document_chunk.shared.result import Result


class ICurriculumExtractor(ABC):
    @abstractmethod
    def extract(
        self,
        parsed_doc: ParsedDocument,
        course_id_hint: str | None = None,
    ) -> Result[Curriculum, Exception]:
        ...

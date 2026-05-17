from abc import ABC, abstractmethod

from document_chunk.shared.result import Result


class ILLMClient(ABC):
    @property
    @abstractmethod
    def model_id(self) -> str:
        """Model identifier used for tracing/persistence."""
        ...

    @abstractmethod
    def generate(
        self,
        prompt: str,
        system: str | None = None,
    ) -> Result[str, Exception]:
        """Generate a text response from a prompt."""
        ...

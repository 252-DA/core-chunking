from abc import ABC, abstractmethod
from dataclasses import dataclass

from document_chunk.shared.result import Ok, Result


@dataclass(frozen=True)
class LLMUsage:
    """Token và chi phí của một lần gọi model."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


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

    def generate_with_usage(
        self,
        prompt: str,
        system: str | None = None,
    ) -> Result[tuple[str, LLMUsage], Exception]:
        """
        Như ``generate`` nhưng kèm token đã dùng.

        Mặc định trả usage rỗng để adapter cũ vẫn chạy; adapter nào đọc được
        usage từ provider thì override. Ghi 0 token vào ``llm_usage_logs`` là
        vô nghĩa, nên chỗ gọi cần phân biệt "không có số liệu" với "0 token".
        """
        result = self.generate(prompt, system=system)
        if result.is_err():
            return result
        return Ok((result.unwrap(), LLMUsage()))

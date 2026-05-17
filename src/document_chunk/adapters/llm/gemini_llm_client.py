from functools import cached_property

from document_chunk.domain.exceptions import LLMError
from document_chunk.domain.ports.llm_client import ILLMClient
from document_chunk.infrastructure.config import LlmConfig
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)


class GeminiLLMClient(ILLMClient):
    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    @property
    def model_id(self) -> str:
        return self._config.model

    @cached_property
    def _sdk(self):
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ImportError(
                "google-genai is not installed. Install dependency: google-genai"
            ) from exc
        return genai, types

    @cached_property
    def _client(self):
        genai, types = self._sdk
        http_options = types.HttpOptions(timeout=self._config.timeout_seconds * 1000)
        return genai.Client(
            api_key=self._config.api_key,
            http_options=http_options,
        )

    def generate(
        self,
        prompt: str,
        system: str | None = None,
    ) -> Result[str, Exception]:
        if not self._config.api_key:
            return Err(LLMError("LLM API key is not configured"))

        try:
            _, types = self._sdk
            config = types.GenerateContentConfig(
                system_instruction=system,
                temperature=self._config.temperature,
            )
            response = self._client.models.generate_content(
                model=self._config.model,
                contents=prompt,
                config=config,
            )
            text = getattr(response, "text", None)
            if text and text.strip():
                return Ok(text.strip())
            return Err(LLMError("Gemini returned an empty response"))
        except Exception as exc:
            logger.error(
                "llm.gemini.generate_failed",
                model=self._config.model,
                error=str(exc),
            )
            return Err(LLMError("Gemini generation failed", cause=exc))

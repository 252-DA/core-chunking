from __future__ import annotations

from document_chunk.adapters.metadata.postgres_repository_utils import (
    PostgresRepositoryBase,
    stable_uuid,
)
from document_chunk.domain.exceptions import MetadataStoreError
from document_chunk.shared.result import Err, Ok, Result


class PostgresLlmUsageRepository(PostgresRepositoryBase):
    """Cost/latency observability for LLM calls (llm_usage_logs)."""

    def record_llm_usage(
        self,
        *,
        provider: str,
        model: str,
        use_case: str,
        status: str,
        course_id: str | None = None,
        user_id: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0,
        latency_ms: int | None = None,
        trace_id: str | None = None,
    ) -> Result[None, Exception]:
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO llm_usage_logs (
                            user_id, course_id, provider, model, use_case,
                            prompt_tokens, completion_tokens, cost_usd,
                            latency_ms, status, trace_id
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s
                        );
                        """,
                        (
                            stable_uuid(user_id) if user_id else None,
                            stable_uuid(course_id) if course_id else None,
                            provider,
                            model,
                            use_case,
                            prompt_tokens,
                            completion_tokens,
                            cost_usd,
                            latency_ms,
                            status,
                            trace_id,
                        ),
                    )
                conn.commit()
            return Ok(None)
        except Exception as exc:
            return Err(MetadataStoreError("PostgreSQL llm_usage_logs insert failed", cause=exc))

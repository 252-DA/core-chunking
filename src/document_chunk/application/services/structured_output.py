"""Generate and validate schema-backed JSON responses from an LLM."""

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from document_chunk.domain.exceptions import ProcessingError
from document_chunk.domain.ports.llm_client import ILLMClient
from document_chunk.shared.result import Err, Ok, Result

SchemaT = TypeVar("SchemaT", bound=BaseModel)

_REPAIR_SYSTEM_PROMPT = "You repair malformed model outputs into strict JSON."


def extract_json_block(raw_text: str) -> str:
    """Return JSON content, unwrapping an optional Markdown code fence."""
    trimmed = raw_text.strip()
    fenced_match = re.search(r"```(?:json)?\s*(.*?)```", trimmed, flags=re.DOTALL)
    if fenced_match:
        return fenced_match.group(1).strip()
    return trimmed


def parse_model_output(
    raw_text: str,
    schema_model: type[SchemaT],
) -> Result[SchemaT, Exception]:
    """Decode LLM text and validate it against the requested Pydantic model."""
    try:
        payload = json.loads(extract_json_block(raw_text))
        return Ok(schema_model.model_validate(payload))
    except (json.JSONDecodeError, ValidationError) as exc:
        return Err(ProcessingError("Invalid structured output from LLM", cause=exc))


def generate_structured_payload(
    llm_client: ILLMClient,
    prompt: str,
    system: str | None,
    schema_model: type[SchemaT],
    repair_schema_name: str | None = None,
) -> Result[SchemaT, Exception]:
    """Generate a typed payload, retrying once with the model's JSON Schema."""
    first_result = llm_client.generate(prompt, system=system)
    if isinstance(first_result, Err):
        return Err(first_result.error)

    first_output = first_result.unwrap()
    parsed_result = parse_model_output(first_output, schema_model)
    if isinstance(parsed_result, Ok):
        return parsed_result

    repair_prompt = _build_repair_prompt(
        invalid_output=first_output,
        schema_model=schema_model,
        schema_name=repair_schema_name,
    )
    repair_result = llm_client.generate(repair_prompt, system=_REPAIR_SYSTEM_PROMPT)
    if isinstance(repair_result, Err):
        return Err(repair_result.error)

    repaired_result = parse_model_output(repair_result.unwrap(), schema_model)
    if isinstance(repaired_result, Ok):
        return repaired_result

    assert isinstance(repaired_result, Err)

    output_label = f"{repair_schema_name} JSON" if repair_schema_name else "JSON"
    return Err(
        ProcessingError(
            f"LLM returned invalid {output_label} after one repair attempt",
            cause=repaired_result.error,
        )
    )


def _build_repair_prompt(
    invalid_output: str,
    schema_model: type[BaseModel],
    schema_name: str | None,
) -> str:
    schema_label = f"Target schema ({schema_name})" if schema_name else "Target schema"
    schema_json = json.dumps(
        schema_model.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "Rewrite the following content into strict JSON only.\n"
        f"{schema_label}: {schema_json}\n"
        "Do not add markdown fences or explanations.\n\n"
        f"{invalid_output}"
    )


__all__ = [
    "extract_json_block",
    "generate_structured_payload",
    "parse_model_output",
]

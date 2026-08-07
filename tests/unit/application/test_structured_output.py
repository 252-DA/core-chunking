"""Tests for the shared schema-backed LLM output service."""

import json
from unittest.mock import MagicMock

from pydantic import BaseModel

from document_chunk.application.services.structured_output import (
    extract_json_block,
    generate_structured_payload,
    parse_model_output,
)
from document_chunk.domain.exceptions import ProcessingError
from document_chunk.shared.result import Err, Ok


class _TestModel(BaseModel):
    name: str
    age: int


class TestExtractJsonBlock:
    def test_pure_json(self):
        assert extract_json_block('{"name":"Alice","age":30}') == '{"name":"Alice","age":30}'

    def test_json_with_whitespace(self):
        assert extract_json_block('  \n{"name":"A"}\n  ') == '{"name":"A"}'

    def test_fenced_json(self):
        raw = '```json\n{"name":"Bob","age":25}\n```'
        assert extract_json_block(raw) == '{"name":"Bob","age":25}'

    def test_fenced_without_language(self):
        raw = '```\n{"name":"C"}\n```'
        assert extract_json_block(raw) == '{"name":"C"}'

    def test_fenced_json_with_surrounding_text(self):
        raw = 'Here is the output:\n```json\n{"name":"D"}\n```\nHope this helps'
        assert extract_json_block(raw) == '{"name":"D"}'

    def test_non_fenced_content_is_trimmed(self):
        assert extract_json_block("  just some text  ") == "just some text"


class TestParseModelOutput:
    def test_valid_json_returns_concrete_model(self):
        result = parse_model_output('{"name":"Alice","age":30}', _TestModel)

        assert result.is_ok()
        assert isinstance(result.unwrap(), _TestModel)
        assert result.unwrap().name == "Alice"
        assert result.unwrap().age == 30

    def test_fenced_json_is_supported(self):
        result = parse_model_output('```json\n{"name":"Bob","age":25}\n```', _TestModel)

        assert result.is_ok()
        assert result.unwrap().name == "Bob"

    def test_invalid_json_returns_processing_error(self):
        result = parse_model_output("not json at all", _TestModel)

        assert result.is_err()
        assert isinstance(result.error, ProcessingError)
        assert isinstance(result.error.cause, json.JSONDecodeError)

    def test_schema_validation_failure_returns_processing_error(self):
        result = parse_model_output('{"name":"Alice"}', _TestModel)

        assert result.is_err()
        assert isinstance(result.error, ProcessingError)

    def test_extra_fields_follow_pydantic_model_policy(self):
        result = parse_model_output(
            '{"name":"Alice","age":30,"extra":"field"}',
            _TestModel,
        )

        assert result.is_ok()
        assert result.unwrap().name == "Alice"


class TestGenerateStructuredPayload:
    def test_first_attempt_succeeds_without_repair(self):
        llm = MagicMock()
        llm.generate.return_value = Ok('{"name":"Alice","age":30}')

        result = generate_structured_payload(
            llm_client=llm,
            prompt="Give me JSON",
            system="Be JSON",
            schema_model=_TestModel,
        )

        assert result.is_ok()
        assert isinstance(result.unwrap(), _TestModel)
        assert llm.generate.call_count == 1

    def test_initial_generation_failure_is_propagated(self):
        llm = MagicMock()
        failure = RuntimeError("llm down")
        llm.generate.return_value = Err(failure)

        result = generate_structured_payload(
            llm_client=llm,
            prompt="Give me JSON",
            system="Be JSON",
            schema_model=_TestModel,
        )

        assert result.is_err()
        assert result.error is failure
        assert llm.generate.call_count == 1

    def test_invalid_json_triggers_one_successful_repair(self):
        llm = MagicMock()
        llm.generate.side_effect = [
            Ok("not valid json"),
            Ok('{"name":"Repaired","age":99}'),
        ]

        result = generate_structured_payload(
            llm_client=llm,
            prompt="Give me JSON",
            system="Be JSON",
            schema_model=_TestModel,
        )

        assert result.is_ok()
        assert result.unwrap().name == "Repaired"
        assert llm.generate.call_count == 2

        repair_call = llm.generate.call_args_list[1]
        repair_prompt = repair_call.args[0]
        serialized_schema = repair_prompt.split("Target schema: ", 1)[1].split("\n", 1)[0]
        schema = json.loads(serialized_schema)
        assert schema == _TestModel.model_json_schema()
        assert "not valid json" in repair_prompt
        assert repair_call.kwargs["system"] == (
            "You repair malformed model outputs into strict JSON."
        )

    def test_named_schema_is_reflected_in_repair_prompt(self):
        llm = MagicMock()
        llm.generate.side_effect = [
            Ok("invalid"),
            Ok('{"name":"Repaired","age":99}'),
        ]

        result = generate_structured_payload(
            llm_client=llm,
            prompt="Give me JSON",
            system="Be JSON",
            schema_model=_TestModel,
            repair_schema_name="profile",
        )

        assert result.is_ok()
        assert "Target schema (profile):" in llm.generate.call_args_list[1].args[0]

    def test_invalid_repair_returns_contextual_processing_error(self):
        llm = MagicMock()
        llm.generate.side_effect = [Ok("invalid #1"), Ok("invalid #2")]

        result = generate_structured_payload(
            llm_client=llm,
            prompt="Give me JSON",
            system="Be JSON",
            schema_model=_TestModel,
            repair_schema_name="profile",
        )

        assert result.is_err()
        assert isinstance(result.error, ProcessingError)
        assert str(result.error).startswith(
            "LLM returned invalid profile JSON after one repair attempt"
        )
        assert llm.generate.call_count == 2

    def test_invalid_repair_without_name_preserves_generic_error(self):
        llm = MagicMock()
        llm.generate.side_effect = [Ok("invalid #1"), Ok("invalid #2")]

        result = generate_structured_payload(
            llm_client=llm,
            prompt="Give me JSON",
            system="Be JSON",
            schema_model=_TestModel,
        )

        assert result.is_err()
        assert str(result.error).startswith(
            "LLM returned invalid JSON after one repair attempt"
        )

    def test_repair_generation_failure_is_propagated(self):
        llm = MagicMock()
        failure = RuntimeError("repair llm down")
        llm.generate.side_effect = [Ok("invalid"), Err(failure)]

        result = generate_structured_payload(
            llm_client=llm,
            prompt="Give me JSON",
            system="Be JSON",
            schema_model=_TestModel,
        )

        assert result.is_err()
        assert result.error is failure
        assert llm.generate.call_count == 2

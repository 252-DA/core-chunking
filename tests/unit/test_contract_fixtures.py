"""
Contract tests: golden fixtures trong contracts/fixtures/v1 được validate bằng
JSON Schema trong contracts/integration-events/v1 và contracts/artifacts/v1.

Cùng bộ fixtures này được dùng bởi TypeScript (Ajv) phía core-api — bất kỳ
thay đổi nào phá schema đều fail ở cả hai phía.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid as uuid_mod
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_DIR = REPO_ROOT / "contracts"

EVENT_SCHEMAS_DIR = CONTRACTS_DIR / "integration-events" / "v1"
ARTIFACT_SCHEMAS_DIR = CONTRACTS_DIR / "artifacts" / "v1"
FIXTURES_DIR = CONTRACTS_DIR / "fixtures" / "v1"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _event_schema_path(event_type: str) -> Path:
    """DOCUMENT_PROCESSING_REQUESTED → document-processing-requested.json"""
    return EVENT_SCHEMAS_DIR / f"{event_type.lower().replace('_', '-')}.json"


def _schema_store() -> dict[str, dict]:
    """Store keyed by schema $id (URL) để $ref cross-file resolve được."""
    store: dict[str, dict] = {}
    for schema_dir in (EVENT_SCHEMAS_DIR, ARTIFACT_SCHEMAS_DIR):
        for path in sorted(schema_dir.glob("*.json")):
            schema = _load_json(path)
            store[schema["$id"]] = schema
    return store


_FORMAT_CHECKER = jsonschema.Draft7Validator.FORMAT_CHECKER


@_FORMAT_CHECKER.checks("uuid")
def _check_uuid(value: object) -> bool:
    try:
        uuid_mod.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


@_FORMAT_CHECKER.checks("date-time")
def _check_date_time(value: object) -> bool:
    try:
        datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _validator_for(schema: dict) -> jsonschema.protocols.Validator:
    """Validator với ref resolver + format checker (uuid/date-time) cho $ref cross-file."""
    store = _schema_store()
    resolver = jsonschema.RefResolver.from_schema(schema, store=store)
    return jsonschema.Draft7Validator(
        schema, resolver=resolver, format_checker=_FORMAT_CHECKER
    )


# ---------------------------------------------------------------------------
# Fixture inventory
# ---------------------------------------------------------------------------

VALID_FIXTURES = sorted((FIXTURES_DIR / "valid").glob("*.json"))
INVALID_FIXTURES = sorted((FIXTURES_DIR / "invalid").glob("*.json"))


def test_all_valid_fixtures_have_matching_schema() -> None:
    # Mỗi valid fixture phải có schema tương ứng (event type hoặc artifact-*)
    for path in VALID_FIXTURES:
        fixture = _load_json(path)
        if "event_type" in fixture:
            expected = _event_schema_path(fixture["event_type"])
            assert expected.exists(), f"{path.name}: thiếu schema {expected.name}"
        else:
            # artifact fixtures: artifact-<kind>.json → <kind>.json
            kind = path.stem.removeprefix("artifact-")
            assert (ARTIFACT_SCHEMAS_DIR / f"{kind}.json").exists(), (
                f"{path.name}: thiếu artifact schema {kind}.json"
            )


def test_all_invalid_fixtures_are_rejected_by_their_schema() -> None:
    for path in INVALID_FIXTURES:
        fixture = _load_json(path)
        event_type = fixture.get("event_type")
        if event_type:
            schema = _load_json(_event_schema_path(event_type))
        else:
            # artifact-invalid fixtures: artifact-oversized → artifact-pointer schema
            if "artifact" in fixture.get("payload", {}) or "compressed_size" in json.dumps(fixture):
                schema = _load_json(ARTIFACT_SCHEMAS_DIR / "artifact-pointer.json")
            else:
                schema = _load_json(ARTIFACT_SCHEMAS_DIR / "content-generation.json")
        validator = _validator_for(schema)
        errors = list(validator.iter_errors(fixture))
        assert errors, f"{path.name}: fixture invalid nhưng schema không reject"


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.name)
def test_valid_fixture_passes_schema(path: Path) -> None:
    fixture = _load_json(path)
    if "event_type" in fixture:
        schema = _load_json(_event_schema_path(fixture["event_type"]))
    else:
        kind = path.stem.removeprefix("artifact-")
        schema = _load_json(ARTIFACT_SCHEMAS_DIR / f"{kind}.json")
    validator = _validator_for(schema)
    errors = list(validator.iter_errors(fixture))
    assert not errors, f"{path.name}: {[e.message for e in errors]}"


# ---------------------------------------------------------------------------
# Canonical checksum — phải khớp giữa Python và TypeScript (Ajv side)
# ---------------------------------------------------------------------------

def canonical_json_bytes(data: object) -> bytes:
    """Canonical JSON UTF-8: sort key đệ quy, không whitespace thừa."""
    def _canon(obj: object):
        if isinstance(obj, dict):
            return {k: _canon(obj[k]) for k in sorted(obj)}
        if isinstance(obj, list):
            return [_canon(v) for v in obj]
        return obj

    return json.dumps(
        _canon(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def test_canonical_checksum_deterministic() -> None:
    fixture = _load_json(FIXTURES_DIR / "valid" / "document-deleted.json")
    h1 = hashlib.sha256(canonical_json_bytes(fixture)).hexdigest()
    # reorder keys intentionally — checksum phải giống
    reordered = {k: fixture[k] for k in reversed(list(fixture.keys()))}
    h2 = hashlib.sha256(canonical_json_bytes(reordered)).hexdigest()
    assert h1 == h2
    assert len(h1) == 64


def test_canonical_checksum_64_char_hex() -> None:
    """Checksum trong artifact pointer phải là sha256 hex 64 ký tự."""
    fixture = _load_json(FIXTURES_DIR / "valid" / "document-indexed.json")
    artifact = fixture["payload"]["artifact"]
    assert len(artifact["sha256"]) == 64
    int(artifact["sha256"], 16)

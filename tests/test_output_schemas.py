"""Contract acceptance and rejection, including MCP image/error transport."""

import pytest
from jsonschema import Draft202012Validator, ValidationError

from nx_mcp.integration_server import IntegrationEnvelope, envelope
from nx_mcp.output_schemas import COUNT, PAYLOADS, S, arr, obj, output_schema
from nx_mcp.runtime import NXToolError


def validate(name, payload):
    schema = output_schema(name, IntegrationEnvelope.model_json_schema())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(envelope(payload).structuredContent)


def test_success_requires_payload_but_error_has_independent_contract(monkeypatch):
    name = "nx_test_list"
    monkeypatch.setitem(PAYLOADS, name, obj({"items": arr(S), "count": COUNT}))
    with pytest.raises(ValidationError):
        validate(name, {})
    validate(
        name,
        NXToolError(
            "NX_TEST", "Actionable failure", details={"mutation_outcome": "rolled_back"}
        ).as_dict(),
    )
    with pytest.raises(ValidationError):
        validate(name, {"status": "error", "message": "missing code and retry guidance"})

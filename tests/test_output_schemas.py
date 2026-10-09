"""Contract acceptance and rejection, including MCP image/error transport."""

from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator, ValidationError
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.integration_server import IntegrationEnvelope, envelope
from nx_mcp.output_schemas import PAYLOADS, output_schema
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace


def validate(name, payload):
    schema = output_schema(name, IntegrationEnvelope.model_json_schema())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(envelope(payload).structuredContent)


@pytest.mark.parametrize("name", list(PAYLOADS))
def test_success_requires_payload_but_error_has_independent_contract(name):
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


@pytest.mark.asyncio
async def test_fresh_mcp_client_gets_structured_errors(tmp_path):
    server = create_server(AsyncMock(), Workspace(tmp_path), surface="full")
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "object_id" in tools["nx_rename_object"].inputSchema["required"]
        bad = await client.call_tool("nx_rename_object", {})
        assert bad.isError and bad.structuredContent["code"] == "NX_INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_new_inspection_tools_publish_actionable_docstrings(tmp_path):
    server = create_server(AsyncMock(), Workspace(tmp_path), surface="full")
    tools = {t.name: t for t in await server.list_tools()}
    expected = {
        "nx_list_datums": "coordinate systems",
    }
    for name, guidance in expected.items():
        assert guidance in tools[name].description
        assert (
            "semantics and installed API support have not been validated"
            not in tools[name].description
        )

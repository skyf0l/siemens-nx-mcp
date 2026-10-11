"""The call keys ``part`` and ``expires_at`` on the sidecar path (phase P5).

The sidecar path is the source of truth for what an agent's ``part`` reaches NX with:
inside ``nx_invoke``/``nx_invoke_read`` arguments, and on the direct core tools, whose
strict listed schemas leave it out (the agent listing has no room) while the server
takes it (``validate_input=False``; Claude Code sends such a key, checked live in P5).
A changing call gets ``expires_at`` (the ghost guard) unless the agent gave one.
"""

import time
from unittest.mock import AsyncMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.bridge import QUEUE_TIMEOUT
from nx_mcp.core import catalogue, evidence
from nx_mcp.core.types import RefKind
from nx_mcp.core.validate import unwrap
from nx_mcp.server import create_server
from nx_mcp.sidecar import server as surface
from nx_mcp.workspace import Workspace
from tests.sidecar.test_budget import AGENT_LISTING, listing_size

BLOCK = {"shape": "block", "length": 40, "width": 20, "height": 10}
REF_LESS = {
    "nx_list_components",
    "nx_list_objects",
    "nx_get_model_summary",
    "nx_find_geometry",
    "nx_check_model",
    "nx_capture_view",
    "nx_undo_operation",
}


@pytest.fixture
def every_tool_tested(monkeypatch):
    specs = catalogue.load()
    tested = {n: {"v2406": {"variants": list(s.variant_values())}} for n, s in specs.items()}
    monkeypatch.setattr(evidence, "_CACHE", tested)
    monkeypatch.delenv("NX_MCP_DEV", raising=False)


def answer(tool, effect="read"):
    return {
        "ok": True,
        "tool": tool,
        "mutation_outcome": "not_applicable" if effect == "read" else "committed",
        "data": {},
        "warnings": [],
    }


def bridge_for(tool, effect="read"):
    bridge = AsyncMock()
    bridge.call.return_value = answer(tool, effect)
    return bridge


def make(tmp_path, bridge, surface_name="agent"):
    return create_server(bridge, Workspace(tmp_path), surface=surface_name, build="v2406")


def ref_less(spec) -> bool:
    """A core tool that acts on a loaded part and can be called without references."""
    if spec.runs != "nx" or spec.target in ("none", "creates", "any"):
        return False
    if spec.target.startswith("arg:"):
        return False
    return not any(p.required and RefKind in unwrap(p.hint)[1] for p in spec.params.values())


def test_the_ref_less_core_tools_are_the_seven_the_instructions_name():
    specs = catalogue.load()
    found = {n for n in surface.CORE_TOOLS if n in specs and ref_less(specs[n])}
    assert found == REF_LESS
    text = surface.AGENT_INSTRUCTIONS
    assert "core tools included" in text and '{"part": "housing", ...}' in text
    assert "nx_list_parts" in text and "agent" not in text.split("When two or more")[1]


async def test_listing_stays_strict_and_within_budget(tmp_path, every_tool_tested):
    """Option (b'): no ``part`` property in the listing, which keeps 14,991 characters;
    a bare property on the seven would cost 175 more (15,166 > 15,000)."""
    tools = await make(tmp_path, AsyncMock()).list_tools()
    assert listing_size(tools) <= AGENT_LISTING
    for tool in tools:
        if tool.name in REF_LESS:
            assert tool.inputSchema["additionalProperties"] is False
            assert "part" not in tool.inputSchema["properties"]


@pytest.mark.parametrize("name", sorted(REF_LESS))
async def test_a_direct_core_tool_takes_part(tmp_path, every_tool_tested, name):
    effect = catalogue.load()[name].effect
    bridge = bridge_for(name, effect)
    args = {"kind": "body"} if name == "nx_list_objects" else {}
    result = await make(tmp_path, bridge).call_tool(name, {**args, "part": "housing"})
    assert not result.isError, result.structuredContent
    method, params = bridge.call.call_args.args
    assert method == name and params["part"] == "housing"


async def test_part_reaches_nx_through_a_real_mcp_client_session(tmp_path, every_tool_tested):
    """The MCP server layer does not validate input against the strict schema."""
    bridge = bridge_for("nx_check_model")
    async with create_connected_server_and_client_session(make(tmp_path, bridge)) as client:
        result = await client.call_tool("nx_check_model", {"part": "cover.prt", "limit": 1})
    assert not result.isError
    assert bridge.call.call_args.args == ("nx_check_model", {"limit": 1, "part": "cover.prt"})


async def test_gateways_forward_both_keys_untouched(tmp_path, every_tool_tested):
    bridge = bridge_for("nx_check_model")
    server = make(tmp_path, bridge)
    keys = {"part": "p:3", "expires_at": "2099-01-01T00:00:00Z"}
    result = await server.call_tool(
        "nx_invoke_read", {"tool": "nx_check_model", "arguments": {"scope": "part", **keys}}
    )
    assert not result.isError
    assert bridge.call.call_args.args[1] == {"scope": "part", **keys}
    bridge.call.return_value = answer("nx_create_primitive", "model")
    await server.call_tool(
        "nx_invoke",
        {
            "tool": "nx_create_primitive",
            "arguments": {**BLOCK, "part": "housing"},
        },
    )
    method, params = bridge.call.call_args.args
    assert method == "nx_create_primitive" and params["part"] == "housing"


async def test_a_changing_call_gets_the_start_deadline_unless_it_has_one(
    tmp_path, every_tool_tested
):
    bridge = bridge_for("nx_undo_operation", "model")
    server = make(tmp_path, bridge)
    before = time.time()
    await server.call_tool("nx_undo_operation", {"part": "housing"})
    expires = bridge.call.call_args.args[1]["expires_at"]
    assert before + QUEUE_TIMEOUT - 0.01 <= expires <= time.time() + QUEUE_TIMEOUT + 1
    await server.call_tool("nx_undo_operation", {"expires_at": 1_900_000_000})
    assert bridge.call.call_args.args[1]["expires_at"] == 1_900_000_000  # the agent's own
    bridge.call.return_value = answer("nx_check_model")
    await server.call_tool("nx_check_model", {})
    assert "expires_at" not in bridge.call.call_args.args[1]  # reads always answer


async def test_a_full_profile_tool_takes_part_too(tmp_path, every_tool_tested):
    bridge = bridge_for("nx_create_primitive", "model")
    result = await make(tmp_path, bridge, "full").call_tool(
        "nx_create_primitive", {**BLOCK, "part": "cover"}
    )
    assert not result.isError, result.structuredContent
    assert bridge.call.call_args.args[1]["part"] == "cover"

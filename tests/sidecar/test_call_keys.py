"""The call keys ``part``, ``expires_at`` and ``caller`` on the sidecar path (phase P5).

The sidecar path is the source of truth for what an agent's ``part`` reaches NX with:
inside ``nx_invoke``/``nx_invoke_read`` arguments, and on the direct core tools, whose
listed schemas name ``part`` on the seven ref-less ones (agents trust a strict schema
over the instructions). The proxy pops it as a call key before gate 1.
A changing call gets ``expires_at`` (the ghost guard) unless the agent gave one. Every NX
call gets ``caller`` (core/busy.py): the sidecar's, never the agent's.
"""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.bridge import QUEUE_TIMEOUT
from nx_mcp.core import catalogue, evidence
from nx_mcp.core.types import RefKind
from nx_mcp.core.validate import unwrap
from nx_mcp.server import create_server
from nx_mcp.sidecar import proxy
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
    assert found == {
        n for n in surface.CORE_TOOLS if n in specs and surface.takes_part_key(specs[n])
    }
    text = surface.AGENT_INSTRUCTIONS
    assert "core tools included" in text and '{"part": "housing", ...}' in text
    assert "leave part out" not in text
    assert "nx_list_parts" in text and "agent" not in text.split("When two or more")[1]


async def test_the_ref_less_core_tools_list_part_within_budget(tmp_path, every_tool_tested):
    """Listed ``part``: the strict schemas name it, so agents pass it."""
    tools = await make(tmp_path, AsyncMock()).list_tools()
    assert listing_size(tools) <= AGENT_LISTING
    for tool in tools:
        properties = tool.inputSchema.get("properties", {})
        if tool.name in REF_LESS:
            assert tool.inputSchema["additionalProperties"] is False
            assert properties["part"] == {
                "type": "string",
                "description": "Target part's file name; needed when 2+ parts are loaded",
            }
        elif tool.name != "nx_open_part":
            assert "part" not in properties, tool.name
    full = {t.name: t for t in await make(tmp_path, AsyncMock(), "full").list_tools()}
    assert "part" in full["nx_check_model"].inputSchema["properties"]


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
    method, params = bridge.call.call_args.args
    caller = params.pop("caller")
    assert (method, params) == ("nx_check_model", {"limit": 1, "part": "cover.prt"})
    assert caller["id"].startswith(proxy.PROCESS_ID + ".")  # this process and MCP session
    assert caller["shared"] is False


async def test_gateways_forward_both_keys_untouched(tmp_path, every_tool_tested):
    bridge = bridge_for("nx_check_model")
    server = make(tmp_path, bridge)
    keys = {"part": "p:3", "expires_at": "2099-01-01T00:00:00Z"}
    result = await server.call_tool(
        "nx_invoke_read", {"tool": "nx_check_model", "arguments": {"scope": "part", **keys}}
    )
    assert not result.isError
    params = dict(bridge.call.call_args.args[1])
    assert params.pop("caller")["id"] == proxy.PROCESS_ID
    assert params == {"scope": "part", **keys}
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


async def test_the_caller_key_is_the_sidecars_never_the_agents(tmp_path, every_tool_tested):
    bridge = bridge_for("nx_create_primitive", "model")
    server = make(tmp_path, bridge)
    forged = {"id": "someone-else", "shared": False}
    await server.call_tool("nx_create_primitive", {**BLOCK, "caller": forged})
    assert bridge.call.call_args.args[1]["caller"] == {"id": proxy.PROCESS_ID, "shared": False}


async def test_overlapping_calls_of_one_caller_mark_its_connection_shared(
    tmp_path, every_tool_tested
):
    """Subagents of one client share its MCP server: their calls overlap."""
    seen: list[dict] = []
    gate = asyncio.Event()

    async def slow(method, params):
        seen.append(dict(params["caller"]))
        await gate.wait()
        return answer(method)

    bridge = AsyncMock()
    bridge.call.side_effect = slow
    server = make(tmp_path, bridge)
    first = asyncio.ensure_future(server.call_tool("nx_check_model", {}))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(server.call_tool("nx_check_model", {}))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(first, second)
    assert [c["shared"] for c in seen] == [False, True]
    await server.call_tool("nx_check_model", {})  # alone, but within SHARED_SECONDS
    assert seen[-1]["shared"] is True


def test_a_client_supplied_agent_id_in_meta_tells_callers_apart():
    from types import SimpleNamespace as NS

    class Meta:
        def __init__(self, **values):
            self.values = values

        def model_dump(self):
            return dict(self.values)

    class Session:  # an MCP ServerSession: one per connection
        pass

    session = Session()
    one = proxy.caller_id(NS(session=session, meta=Meta(**{"claudecode/agentId": "agent 7!"})))
    two = proxy.caller_id(NS(session=session, meta=Meta(sessionId="s-2", progressToken=3)))
    plain = proxy.caller_id(NS(session=session, meta=None))
    assert one.endswith(":agent7") and two.endswith(":s-2")
    assert one.split(":")[0] == two.split(":")[0] == plain  # same connection
    assert proxy.caller_id(NS(session=Session(), meta=None)) != plain  # another session

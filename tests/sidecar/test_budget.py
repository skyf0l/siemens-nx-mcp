"""Token budgets of the listings (tool-surface 2.2, architecture.md 6), at 3.8 chars/token.

The caps are on what an agent reads: the ``tools/list`` payload of each profile, one
``nx_find_tools`` page and ``nx_get_tool_schemas`` per tool.
"""

import json
import statistics
import typing
from unittest.mock import AsyncMock

import pytest

from nx_mcp.core import catalogue, evidence
from nx_mcp.server import create_server
from nx_mcp.sidecar import schema
from nx_mcp.workspace import Workspace

CHARS_PER_TOKEN = 3.8
# 15,000 until 2026-10-12; raised to list ``part`` on the seven ref-less core tools
# (sidecar/server.py PART_KEY, 7 x 103 characters) and ``sheet`` on nx_capture_view. In
# benchmark bench3p agents trusted the strict schemas over the instruction sentence: with
# ``part`` left out, none of four builders passed it to a core tool (one gave up a
# capture, one routed through nx_invoke_read, the judge closed parts to capture). The raise
# is kept small by dropping text the listing repeated: the offset/limit descriptions
# ("First row to return", "Rows per page") and idempotentHint false (MCP's default).
AGENT_LISTING = 15_300  # 15,251 on 2026-10-12 (12 domains in the find domain enum)
# Full profile cap (tool-surface 2.2): a mean per listed tool, so the cap does not have to
# move each time a tool is added. 157 tools averaged 798.5 characters on 2026-10-10 (name +
# description + input schema, with the 87-character call keys operation_id and intent on
# changing tools; the union schemas of patterns, holes and sheet metal lift the mean above
# the 700 median). On 2026-10-11 165 tools averaged 942.1: the natural-argument forms
# (quantity strings such as '10 mm', unit and name aliases, principal-plane names) widened
# many unions; 1040 leaves about 10% headroom. The per-tool caps below still bound every
# single definition. The full profile is opt-in, for clients that index tools and load
# schemas on demand; the default agent profile keeps its 15,000 total.
FULL_MEAN_PER_TOOL = 1040
FIND_PAGE = 1_400
# The empty-query overview lists every domain (about 120 characters each) rather than a
# page of ten tool rows: 1,400 until 2026-10-12, when motion and optimization made 12
# domains and 1,557 characters; 1,650 leaves about 6% headroom.
OVERVIEW_PAGE = 1_650
SCHEMA_PER_TOOL = 900
MEDIAN_DEFINITION = 700  # 699 over 176 tools on 2026-10-12
DEFINITION_CAP, UNION_CAP = 1_200, 2_500
# A schema entry adds example, requires and evidence to the tool definition.
SCHEMA_OVERHEAD = SCHEMA_PER_TOOL - MEDIAN_DEFINITION


def compact(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def listing_size(tools) -> int:
    return sum(compact(t.model_dump(exclude_none=True)) for t in tools)


def definition(tool) -> int:
    """name + description + input schema, as tool-surface 1.7 measures a definition."""
    return len(tool.name) + len(tool.description or "") + compact(tool.inputSchema)


@pytest.fixture
def every_tool_tested(monkeypatch):
    tested = {name: {"v2406": {"variants": []}} for name in catalogue.load()}
    for name, spec in catalogue.load().items():
        tested[name]["v2406"]["variants"] = list(spec.variant_values())
    monkeypatch.setattr(evidence, "_CACHE", tested)
    monkeypatch.delenv("NX_MCP_DEV", raising=False)


def make(tmp_path, surface):
    return create_server(AsyncMock(), Workspace(tmp_path), surface=surface, build="v2406")


async def test_agent_profile_listing_fits_its_budget(tmp_path, every_tool_tested):
    size = listing_size(await make(tmp_path, "agent").list_tools())
    print(f"agent listing: {size} chars, {size / CHARS_PER_TOKEN:.0f} tokens")
    assert size <= AGENT_LISTING


async def test_full_listing_fits_its_budget(tmp_path, every_tool_tested):
    tools = await make(tmp_path, "full").list_tools()
    size = sum(definition(t) for t in tools)
    mean = size / len(tools)
    print(f"full listing: {size} chars, {len(tools)} tools, mean {mean:.1f}")
    assert mean <= FULL_MEAN_PER_TOOL


def own_definition(spec) -> int:
    """name + description + input schema as ``nx_get_tool_schemas`` returns it.

    The call keys ``operation_id`` and ``intent`` (the same 87 characters on every changing
    tool, explained once in the server instructions) are listed only on directly listed
    tools, so the per-tool caps and the median measure the tool's own definition; the
    full listing's mean cap above counts them.
    """
    generated = schema.input_schema(spec, call_keys=False)
    return (
        len(spec.name) + len(spec.description) + len(json.dumps(generated, separators=(",", ":")))
    )


def has_object_union(generated: dict) -> bool:
    """True when an argument is a union of two or more object forms (Limit, Axis, a
    linear|circular layout): a discriminated union in the sense of tool-surface 1.7 even
    without a top-level variant field, so the 2,500 cap applies."""
    defs = generated.get("$defs", {})

    def is_object(node):
        if "$ref" in node:
            node = defs.get(node["$ref"].split("/")[-1], {})
        return node.get("type") == "object"

    def walk(node):
        if isinstance(node, dict):
            branches = node.get("anyOf")
            if isinstance(branches, list) and sum(is_object(b) for b in branches) >= 2:
                return True
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return False

    return walk(generated)


def test_catalogue_definitions_respect_their_caps():
    sizes = {}
    for spec in catalogue.load().values():
        size = own_definition(spec)
        sizes[spec.name] = size
        union = bool(spec.variants) or has_object_union(schema.input_schema(spec))
        assert size <= (UNION_CAP if union else DEFINITION_CAP), (spec.name, size)
    median = statistics.median(sizes.values())
    print(f"definitions: median {median}, max {max(sizes.values())}:", sizes)
    if len(sizes) >= 10:  # a median of the two pilot tools is not the catalogue's median
        assert median <= MEDIAN_DEFINITION


@pytest.mark.parametrize("query", ["", "datum plane", "create", "list faces", "measure"])
async def test_a_page_of_ten_discovery_rows_fits(tmp_path, every_tool_tested, query):
    result = await make(tmp_path, "agent").call_tool("nx_find_tools", {"query": query})
    text = result.content[0].text
    print(f"find {query!r}: {len(text)} chars")
    data = result.structuredContent["data"]
    if query:
        assert len(data["tools"]) <= 10
        assert len(text) <= FIND_PAGE
    else:  # the domain overview: one row per domain
        assert len(data["domains"]) <= len(typing.get_args(catalogue.Domain))
        assert len(text) <= OVERVIEW_PAGE


async def test_schema_fetch_adds_little_to_each_definition(tmp_path, every_tool_tested):
    server = make(tmp_path, "agent")
    names = sorted(catalogue.load())
    entries, text = [], 0
    for start in range(0, len(names), 10):  # nx_get_tool_schemas takes 1-10 names
        result = await server.call_tool("nx_get_tool_schemas", {"names": names[start : start + 10]})
        entries += result.structuredContent["data"]["tools"]
        text += len(result.content[0].text)
    assert [e["name"] for e in entries] == names
    listed = {t.name: t for t in await make(tmp_path, "full").list_tools()}
    for entry in entries:
        size = compact(entry)
        overhead = size - definition(listed[entry["name"]])
        print(f"schema {entry['name']}: {size} chars, {overhead} over its definition")
        assert overhead <= SCHEMA_OVERHEAD
    average = text / len(entries)
    definitions = statistics.mean(definition(listed[n]) for n in names)
    print(f"schema fetch: {average:.0f} chars per tool on average")
    # 900 per tool on average once definitions meet their 700 median.
    assert average <= SCHEMA_PER_TOOL + max(0, definitions - MEDIAN_DEFINITION)

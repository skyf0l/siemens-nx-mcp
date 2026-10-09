"""Token budgets of the listings (tool-surface 2.2, architecture.md 6), at 3.8 chars/token.

The caps are on what an agent reads: the ``tools/list`` payload of each profile, one
``nx_find_tools`` page and ``nx_get_tool_schemas`` per tool. During migration the
``full`` listing still carries the legacy tools, so its cap applies to the catalogue
part and the legacy part may only shrink (architecture.md section 4, F3 notes).
"""

import json
import statistics
from unittest.mock import AsyncMock

import pytest

from nx_mcp.core import catalogue, evidence
from nx_mcp.server import create_server
from nx_mcp.sidecar import schema
from nx_mcp.workspace import Workspace

CHARS_PER_TOKEN = 3.8
AGENT_LISTING = 15_000
FULL_CATALOGUE = 110_000
LEGACY_TODAY = 218_951  # tool-surface 2.2: the legacy full listing before F3
FIND_PAGE = 1_400
SCHEMA_PER_TOOL = 900
MEDIAN_DEFINITION = 700
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


async def test_full_listing_catalogue_part_and_legacy_part(tmp_path, every_tool_tested):
    server = make(tmp_path, "full")
    tools = await server.list_tools()
    added = server.nx_surface.added_names()
    catalogue_part = sum(definition(t) for t in tools if t.name in added)
    legacy_part = sum(definition(t) for t in tools if t.name not in added)
    print(f"full listing: catalogue {catalogue_part}, legacy {legacy_part} chars")
    assert catalogue_part <= FULL_CATALOGUE
    assert legacy_part <= LEGACY_TODAY


def test_catalogue_definitions_respect_their_caps():
    sizes = {}
    for spec in catalogue.load().values():
        size = schema.definition_size(spec.name, spec.description, schema.input_schema(spec))
        sizes[spec.name] = size
        assert size <= (UNION_CAP if spec.variants else DEFINITION_CAP), (spec.name, size)
    print("definitions:", sizes)
    if len(sizes) >= 10:  # a median of the two pilot tools is not the catalogue's median
        assert statistics.median(sizes.values()) <= MEDIAN_DEFINITION


@pytest.mark.parametrize("query", ["", "datum plane", "create", "list faces", "measure"])
async def test_a_page_of_ten_discovery_rows_fits(tmp_path, every_tool_tested, query):
    result = await make(tmp_path, "agent").call_tool("nx_find_tools", {"query": query})
    text = result.content[0].text
    print(f"find {query!r}: {len(text)} chars")
    assert len(result.structuredContent["data"]["tools"]) <= 10
    assert len(text) <= FIND_PAGE


async def test_schema_fetch_adds_little_to_each_definition(tmp_path, every_tool_tested):
    server = make(tmp_path, "agent")
    names = sorted(catalogue.load())
    result = await server.call_tool("nx_get_tool_schemas", {"names": names})
    entries = result.structuredContent["data"]["tools"]
    listed = {t.name: t for t in await make(tmp_path, "full").list_tools()}
    for entry in entries:
        size = compact(entry)
        overhead = size - definition(listed[entry["name"]])
        print(f"schema {entry['name']}: {size} chars, {overhead} over its definition")
        assert overhead <= SCHEMA_OVERHEAD
    average = len(result.content[0].text) / len(entries)
    definitions = statistics.mean(definition(listed[n]) for n in names)
    # 900 per tool on average once definitions meet their 700 median.
    assert average <= SCHEMA_PER_TOOL + max(0, definitions - MEDIAN_DEFINITION)

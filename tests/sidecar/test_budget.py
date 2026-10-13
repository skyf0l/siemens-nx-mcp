"""Token budgets of what an agent reads (tool-surface 1.7 and 2.2, architecture.md 6).

Principle (engineer, 2026-10-13): tools must be explicit and schemas detailed. A failed
call and its retry cost about 1-3K tokens; 100 more schema characters cost about 26 tokens
per fetch. These caps exist to catch bloat (repetition, filler, bytes no agent uses),
never to justify leaving out a rule an agent needs: when a needed rule does not fit, the
cap is what changes, in the same change, with the reason.

Every cap is a measured value plus headroom. A cap moves only when what it measures
changes in kind (a new key on every row, a new section in the listing), never because a
tool or a domain was added: the caps are per tool, per row and per entry, percentiles, or
a formula in the number of domains. :func:`measure` is the one measurement; the table in
architecture.md section 6 is generated from it (``scripts/generate_budget_table.py``) and
``pytest -s`` prints it. Sizes are characters of compact JSON; tokens at 3.8 characters.
"""

from __future__ import annotations

import contextlib
import json
import os
import statistics
import tempfile
import typing
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nx_mcp.core import catalogue, evidence
from nx_mcp.server import create_server
from nx_mcp.sidecar import schema
from nx_mcp.sidecar.server import AGENT_INSTRUCTIONS
from nx_mcp.workspace import Workspace

CHARS_PER_TOKEN = 3.8
DOMAINS = len(typing.get_args(catalogue.Domain))

# -- Per session, paid on every turn ------------------------------------------------------
# The agent listing (4 gateways + 14 core tools). Its only term that grows with the catalogue
# is the nx_find_tools domain enum (about 12-15 characters per domain), hence the formula.
# 2026-10-13: 15,067 characters at 12 domains, the cap about 3% above. 15,251 before: hints
# equal to MCP's defaults, the ref maxLength, the page offset maximum and 5 characters of the
# part key were dropped (about 350), the sentence on grouped variants was added to
# nx_get_tool_schemas (165). History: 15,000 until 2026-10-12, then 15,300 for the part key.
AGENT_LISTING_BASE, PER_DOMAIN = 15_340, 15
AGENT_LISTING = AGENT_LISTING_BASE + PER_DOMAIN * DOMAINS  # 15,520 at 12 domains
# The server instructions, sent once per session: 1,395 on 2026-10-13 (unbudgeted before).
INSTRUCTIONS_CAP = 1_600

# -- Full profile ------------------------------------------------------------------------
# A mean per listed tool (name + description + input schema with the call keys), so adding
# tools does not move it: 977.6 over 180 tools on 2026-10-12, 951.8 after the 2026-10-13
# cuts (kept: 9% headroom). 880 on 2026-10-10 (798.5), 1,040 since the natural-argument
# forms of 2026-10-11 (942.1). Only full-profile clients pay it, so it stays a mean.
FULL_MEAN_PER_TOOL = 1_040

# -- Per tool fetched --------------------------------------------------------------------
# A tool's own definition (name + description + input schema, no call keys) as one
# nx_get_tool_schemas entry carries it. Hard caps per tool (1,200/2,500 until 2026-10-13,
# raised so a rule an agent needs never has to be cut to fit): largest 1,193 plain and
# 2,421 union on 2026-10-13.
DEFINITION_CAP, UNION_CAP = 1_600, 3_000
# Drift: the 75th percentile per kind (a median over all tools moved with the share of
# unions, not with verbose writing). 2026-10-13: plain 866 (121 tools), union 1,657.5 (55
# tools), after the cuts (884 and 1,721 before); caps about 12% above.
PLAIN_P75, UNION_P75 = 970, 1_860

# -- Per discovery call ------------------------------------------------------------------
# nx_find_tools over FIND_QUERIES: each row and the fixed part (wrapper, call_with,
# listing, page, notes) are capped, so neither more tools nor more domains move the caps;
# the 90th percentile page bounds the typical page. 2026-10-13: row max 163, fixed max
# 385, p90 1,461 (max 1,812: the multi-task query with its hint). Until 2026-10-13 a
# 1,400 total on four queries, which 9 of these 26 exceeded.
FIND_ROW, FIND_FIXED, FIND_P90 = 170, 400, 1_640
# The empty-query overview, one row per domain: no total, a row and a fixed cap.
# 2026-10-13: row max 125, fixed 333 (1,557 in all for 12 domains; a 1,650 total until
# then, which the next domain would have broken).
OVERVIEW_ROW, OVERVIEW_FIXED = 140, 375

# -- Schema fetch ------------------------------------------------------------------------
# What an nx_get_tool_schemas entry adds to the tool's definition (effect, example,
# requires, required_by, evidence). 2026-10-13: max 194, mean 74.5 (a 200 cap and a
# 900-per-tool average formula until then).
SCHEMA_OVERHEAD, SCHEMA_OVERHEAD_MEAN = 250, 85

# -- Per task ----------------------------------------------------------------------------
# A typical modeling task's discovery: the overview, three searches and four schemas in one
# fetch (TYPICAL_FINDS, TYPICAL_SCHEMAS). 2026-10-13: 12,202 characters, about 3.2K tokens.
TYPICAL_TASK = 13_650

# One task per query, as agents search (budget review 2026-10-13): a word, two words, a
# typo, a multi-task sentence, words of other CAD systems.
FIND_QUERIES = (
    "datum plane",
    "create",
    "list faces",
    "measure",
    "sheet metal flange",
    "drawing dimension",
    "blend edges",
    "export step",
    "assembly constraint",
    "revolve profile",
    "sketch polyline",
    "hole",
    "pattern feature",
    "save part",
    "mass properties",
    "section view",
    "extrdue",
    "fillet and chamfer",
    "add component to assembly then constrain it",
    "thicken surface",
    "motion",
    "topology",
    "camera view",
    "expression",
    "open part",
    "flat pattern",
)
TYPICAL_FINDS = ("", "sketch", "extrude", "blend edges")
TYPICAL_SCHEMAS = (
    "nx_create_sketch",
    "nx_create_sketch_curves",
    "nx_create_extrude",
    "nx_create_blend",
)


def compact(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def listing_size(tools) -> int:
    return sum(compact(t.model_dump(exclude_none=True)) for t in tools)


def definition(tool) -> int:
    """name + description + input schema, as tool-surface 1.7 measures a definition."""
    return len(tool.name) + len(tool.description or "") + compact(tool.inputSchema)


def own_definition(spec) -> int:
    """name + description + input schema as ``nx_get_tool_schemas`` returns it.

    The call keys ``operation_id`` and ``intent`` (the same 87 characters on every changing
    tool, explained once in the server instructions) are listed only on directly listed
    tools, so the per-tool caps and percentiles measure the tool's own definition; the
    full listing's mean cap counts them.
    """
    generated = schema.input_schema(spec, call_keys=False)
    return (
        len(spec.name) + len(spec.description) + len(json.dumps(generated, separators=(",", ":")))
    )


def has_object_union(generated: dict) -> bool:
    """True when an argument is a union of two or more object forms (Limit, Axis, a
    linear|circular layout): a discriminated union in the sense of tool-surface 1.7 even
    without a top-level variant field, so the union cap applies."""
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


def is_union(spec) -> bool:
    return bool(spec.variants) or has_object_union(schema.input_schema(spec))


def p75(values) -> float:
    return statistics.quantiles(values, n=4, method="inclusive")[2]


def p90(values) -> float:
    return statistics.quantiles(values, n=10, method="inclusive")[8]


@contextlib.contextmanager
def every_tool_tested():
    """Listings as on a build where every tool and variant has evidence (not dev mode)."""
    saved, dev = evidence._CACHE, os.environ.pop("NX_MCP_DEV", None)
    evidence._CACHE = {
        name: {"v2406": {"variants": list(spec.variant_values())}}
        for name, spec in catalogue.load().items()
    }
    try:
        yield
    finally:
        evidence._CACHE = saved
        if dev is not None:
            os.environ["NX_MCP_DEV"] = dev


def make(tmp_path, surface):
    return create_server(AsyncMock(), Workspace(tmp_path), surface=surface, build="v2406")


async def call(server, name, arguments) -> tuple[int, dict]:
    """``(characters of the text content, data)`` of a gateway call."""
    result = await server.call_tool(name, arguments)
    return len(result.content[0].text), result.structuredContent["data"]


def parts(text: int, rows: list) -> tuple[list[int], int]:
    """``(row sizes, the rest)`` of a page: the rest is the page without its rows."""
    sizes = [compact(row) for row in rows]
    return sizes, text - sum(sizes) - max(len(sizes) - 1, 0)  # commas between rows


async def find_pages(server) -> dict[str, tuple[int, list[int], int]]:
    """``{query: (page, row sizes, fixed part)}`` over FIND_QUERIES."""
    pages = {}
    for query in FIND_QUERIES:
        text, data = await call(server, "nx_find_tools", {"query": query})
        assert len(data["tools"]) <= 10
        rows = data["tools"] + data.get("domains", [])  # no hit: the overview's rows too
        pages[query] = (text, *parts(text, rows))
    return pages


async def overview(server) -> tuple[int, list[int], int]:
    text, data = await call(server, "nx_find_tools", {"query": ""})
    assert len(data["domains"]) == DOMAINS
    return (text, *parts(text, data["domains"]))


async def schema_overheads(tmp_path) -> dict[str, int]:
    """What each nx_get_tool_schemas entry adds to the tool's listed definition."""
    server = make(tmp_path, "agent")
    names = sorted(catalogue.load())
    entries = []
    for start in range(0, len(names), 10):  # nx_get_tool_schemas takes 1-10 names
        _, data = await call(server, "nx_get_tool_schemas", {"names": names[start : start + 10]})
        entries += data["tools"]
    assert [e["name"] for e in entries] == names
    listed = {t.name: t for t in await make(tmp_path, "full").list_tools()}
    return {e["name"]: compact(e) - definition(listed[e["name"]]) for e in entries}


async def typical_task(server) -> int:
    total = 0
    for query in TYPICAL_FINDS:
        total += (await call(server, "nx_find_tools", {"query": query}))[0]
    names = list(TYPICAL_SCHEMAS)
    return total + (await call(server, "nx_get_tool_schemas", {"names": names}))[0]


async def measure(tmp_path) -> dict[str, typing.Any]:
    """Every budgeted value (call inside :func:`every_tool_tested`)."""
    agent = make(tmp_path, "agent")
    full = [definition(t) for t in await make(tmp_path, "full").list_tools()]
    own = {"plain": [], "union": []}
    for spec in catalogue.load().values():
        own["union" if is_union(spec) else "plain"].append(own_definition(spec))
    pages = await find_pages(agent)
    page, rows, fixed = await overview(agent)
    overheads = list((await schema_overheads(tmp_path)).values())
    return {
        "domains": DOMAINS,
        "agent_listing": listing_size(await agent.list_tools()),
        "instructions": len(AGENT_INSTRUCTIONS),
        "full_tools": len(full),
        "full_total": sum(full),
        "full_mean": sum(full) / len(full),
        "plain": (len(own["plain"]), p75(own["plain"]), max(own["plain"])),
        "union": (len(own["union"]), p75(own["union"]), max(own["union"])),
        "find_queries": len(pages),
        "find_row_max": max(max(r, default=0) for _, r, _ in pages.values()),
        "find_fixed_max": max(f for _, _, f in pages.values()),
        "find_p90": p90([p for p, _, _ in pages.values()]),
        "find_max": max(p for p, _, _ in pages.values()),
        "overview": page,
        "overview_row_max": max(rows),
        "overview_fixed": fixed,
        "schema_overhead_max": max(overheads),
        "schema_overhead_mean": statistics.mean(overheads),
        "typical_task": await typical_task(agent),
    }


def measured_now() -> dict[str, typing.Any]:
    """:func:`measure` outside pytest (scripts/generate_budget_table.py)."""
    import asyncio

    with every_tool_tested(), tempfile.TemporaryDirectory() as tmp:
        return asyncio.run(measure(Path(tmp)))


@pytest.fixture
def every_tool_tested_fixture():
    with every_tool_tested():
        yield


# -- Tests -------------------------------------------------------------------------------


async def test_agent_listing_and_instructions_fit(tmp_path, every_tool_tested_fixture):
    size = listing_size(await make(tmp_path, "agent").list_tools())
    print(f"agent listing: {size} chars ({size / CHARS_PER_TOKEN:.0f} tokens), cap {AGENT_LISTING}")
    assert size <= AGENT_LISTING
    assert len(AGENT_INSTRUCTIONS) <= INSTRUCTIONS_CAP


async def test_full_listing_mean_fits(tmp_path, every_tool_tested_fixture):
    tools = await make(tmp_path, "full").list_tools()
    mean = sum(definition(t) for t in tools) / len(tools)
    print(f"full listing: {len(tools)} tools, mean {mean:.1f}")
    assert mean <= FULL_MEAN_PER_TOOL


def test_each_definition_fits_and_the_kinds_do_not_drift():
    sizes: dict[str, list[int]] = {"plain": [], "union": []}
    for spec in catalogue.load().values():
        size = own_definition(spec)
        union = is_union(spec)
        assert size <= (UNION_CAP if union else DEFINITION_CAP), (spec.name, size)
        sizes["union" if union else "plain"].append(size)
    for kind, cap in (("plain", PLAIN_P75), ("union", UNION_P75)):
        values = sizes[kind]
        print(f"{kind}: {len(values)} tools, p75 {p75(values):.0f}, max {max(values)}")
        if len(values) >= 10:  # a percentile of a few pilot tools is not the catalogue's
            assert p75(values) <= cap, kind


async def test_find_rows_and_pages_fit(tmp_path, every_tool_tested_fixture):
    pages = await find_pages(make(tmp_path, "agent"))
    for query, (page, rows, fixed) in pages.items():
        print(f"find {query!r}: {page} chars, rows {rows}, fixed {fixed}")
        assert all(row <= FIND_ROW for row in rows), query
        assert fixed <= FIND_FIXED, query
    assert p90([page for page, _, _ in pages.values()]) <= FIND_P90


async def test_overview_rows_fit(tmp_path, every_tool_tested_fixture):
    page, rows, fixed = await overview(make(tmp_path, "agent"))
    print(f"overview: {page} chars, rows {rows}, fixed {fixed}")
    assert max(rows) <= OVERVIEW_ROW and fixed <= OVERVIEW_FIXED


async def test_schema_entries_add_little_to_each_definition(tmp_path, every_tool_tested_fixture):
    overheads = await schema_overheads(tmp_path)
    for name, overhead in overheads.items():
        assert overhead <= SCHEMA_OVERHEAD, (name, overhead)
    mean = statistics.mean(overheads.values())
    print(f"schema fetch: overhead mean {mean:.0f}, max {max(overheads.values())}")
    assert mean <= SCHEMA_OVERHEAD_MEAN


async def test_a_typical_task_fits(tmp_path, every_tool_tested_fixture):
    size = await typical_task(make(tmp_path, "agent"))
    print(f"typical task: {size} chars, {size / CHARS_PER_TOKEN:.0f} tokens")
    assert size <= TYPICAL_TASK

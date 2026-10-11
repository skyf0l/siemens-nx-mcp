"""The MCP surface generated from the catalogue.

``install(mcp, bridge, workspace, profile)`` replaces the ``tools/list`` and
``tools/call`` handlers of the FastMCP server:

- ``agent`` (default): the four gateways plus the core tools of tool-surface
  2.1 that exist in the catalogue;
- ``full``: the gateways and every catalogue tool.

Catalogue tools are listed from their specs (schema, docstring, MCP annotations
from ``effect``) and dispatched through :class:`CatalogueProxy`.

Listings follow the connected NX build (gate 3): tools and variants without
evidence there, and those NX refused for a missing license this session, are
left out of tools/list, nx_find_tools and nx_get_tool_schemas. Before NX
connects they follow the default supported build (``evidence.DEFAULT_BUILD``)
and say so; when that changes, the server sends ``tools/list_changed``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from typing import Any, cast

from mcp.server.lowlevel.server import NotificationOptions
from mcp.types import CallToolResult, Tool, ToolAnnotations
from pydantic import ValidationError

from nx_mcp.core import catalogue, evidence
from nx_mcp.core.catalogue import CatalogueError
from nx_mcp.core.errors import NXError
from nx_mcp.sidecar import artifacts, gateways, schema
from nx_mcp.sidecar.proxy import CatalogueProxy, failure, result

logger = logging.getLogger("nx_mcp")
PROFILES = ("agent", "full")
# tool-surface 2.1: core tools listed directly in the agent profile once they exist.
CORE_TOOLS = (
    "nx_get_status",
    "nx_get_operation",
    "nx_get_result",
    "nx_list_parts",
    "nx_open_part",
    "nx_list_components",
    "nx_list_objects",
    "nx_get_model_summary",
    "nx_find_geometry",
    "nx_measure_distance",
    "nx_check_model",
    "nx_capture_view",
    "nx_undo_operation",
    "nx_download_file",
)
AGENT_INSTRUCTIONS = (
    "NX agent profile. Find tools with nx_find_tools: query takes 1-4 keywords for one task "
    "('revolve profile'); for several tasks pass queries, one task each. Fetch exact schemas with "
    "nx_get_tool_schemas (1-10 names; 'tool#variant' for one variant), then call them through "
    "nx_invoke, or nx_invoke_read for read-only tools: catalogue tools found with nx_find_tools "
    "are not MCP tools of their own (only the listed core tools are). Results carry ok, "
    "mutation_outcome and data, or error {category, code, retry, hint, field}. operation_id is "
    "optional: the server makes one when omitted. Pass your own inside arguments only for a "
    "mutation you may retry: 8-128 characters from A-Z a-z 0-9 _ - (e.g. 'fan_blade_01'); "
    "after an unknown outcome query nx_get_operation before retrying. Compact results keep "
    "result_id; expand the omitted fields with nx_get_result. Never paste base64 file content "
    "into the conversation. Lengths are in part units and angles in degrees; they also take "
    "'10 mm'/'90 deg' or an NX expression name or formula ('thickness', 'thickness/2'), which "
    "keeps extrude, revolve, hole, pattern, blend and chamfer values driven by the expression. "
    "Options match in any case or spacing ('Through All'). When two or more parts are loaded, "
    "add the target part's file name to the arguments of every call without refs, the listed "
    'core tools included (their schemas leave part out): {"part": "housing", ...}; '
    "nx_list_parts lists them."
)
# The ref-less core tools (nx_list_components, nx_list_objects, nx_get_model_summary,
# nx_find_geometry, nx_check_model, nx_capture_view, nx_undo_operation) take ``part``
# directly although their strict listed schemas leave it out (the agent listing has no
# room for it): the server does not validate input against them (install:
# validate_input=False) and the proxy pops the call keys before gate 1. Claude Code sends
# a key a schema leaves out (checked live in phase P5).
CALL_WITH = "nx_invoke; nx_invoke_read for effect read (not as direct MCP tools)"
# nx_find_tools: default rows for one query and per query of ``queries``; a query with more
# words than this, or joining tasks (``,`` ``;`` ``+`` "and" "then"), earns a hint to split
# the tasks into ``queries`` (a long single task, "assembly constraints status", does not).
FIND_LIMIT, QUERIES_LIMIT, MAX_QUERY_WORDS = 10, 5, 5
TASK_JOINS = re.compile(r"[,;+&]|\b(?:and|then|also)\b", re.IGNORECASE)
FULL_INSTRUCTIONS = (
    "Siemens NX integration. Runtime support varies by NX version; no general certification "
    "is claimed. Tools return {ok, mutation_outcome, data | error}. Every changing tool takes "
    "an optional operation_id (8-128 characters from A-Z a-z 0-9 _ -; omitted, the server "
    "makes one; reuse your own to retry safely) and intent (why, in the engineer's terms; "
    "shown in the NX panel). File paths are workspace-relative or absolute inside the "
    "workspace. Lengths and angles also take '10 mm'/'90 deg' or an NX expression name or "
    "formula ('thickness/2')."
)


def tool_annotations(effect: str, name: str, label: str | None = None) -> ToolAnnotations:
    """MCP hints from a spec effect (surface 1.3)."""
    verb = name.split("_")[1] if name.count("_") >= 2 else ""
    return ToolAnnotations(
        title=label,
        readOnlyHint=effect == "read",
        # MCP: destructiveHint means something only for tools that are not read-only.
        destructiveHint=None if effect == "read" else effect in ("model", "session"),
        idempotentHint=effect == "read" or verb == "set",
        openWorldHint=False,
    )


class Surface:
    def __init__(self, mcp, bridge, workspace, profile: str, *, build=None) -> None:
        if profile not in PROFILES:
            raise ValueError("NX_MCP_SURFACE must be full or agent")
        self.mcp = mcp
        self.profile = profile
        self.specs = dict(catalogue.load())
        clashes = sorted(set(self.specs) & set(gateways.NAMES))
        if clashes:
            raise CatalogueError("catalogue tools collide with gateways: " + ", ".join(clashes))
        store = artifacts.install(mcp, workspace)
        self.proxy = CatalogueProxy(bridge, workspace, store, build=build)
        self._listed: tuple | None = None  # proxy.signature() of the last tools/list
        self.entries = [
            gateways.Entry(
                s.name,
                effect=s.effect,
                domain=s.domain,
                label=s.label,
                description=s.description,
                keywords=s.keywords,
            )
            for s in self.specs.values()
        ]
        self.index = gateways.Index(self.entries)  # word rarity over the whole catalogue

    # -- listing ---------------------------------------------------------------

    def gateway_tools(self) -> list[Tool]:
        effects = {"nx_invoke": "model"}
        return [
            Tool(
                name=name,
                description=gateways.DESCRIPTIONS[name],
                inputSchema=gateways.tool_schema(name),
                annotations=tool_annotations(effects.get(name, "read"), name),
            )
            for name in gateways.NAMES
        ]

    def catalogue_tool(self, spec) -> Tool:
        return Tool(
            name=spec.name,
            title=spec.label,
            description=spec.description,
            inputSchema=schema.narrow_variants(
                schema.input_schema(spec), spec, self.proxy.variants(spec)
            ),
            annotations=tool_annotations(spec.effect, spec.name),
        )

    async def list_tools(self) -> list[Tool]:
        await self.proxy.refresh()
        self._listed = self.proxy.signature()
        tools = self.gateway_tools()
        if self.profile == "agent":
            listed = [self.specs[n] for n in CORE_TOOLS if n in self.specs]
        else:
            listed = sorted(self.specs.values(), key=lambda s: s.name)
        tools += [self.catalogue_tool(s) for s in listed if self.proxy.visible(s)]
        return tools

    # -- dispatch --------------------------------------------------------------

    def known(self) -> set[str]:
        return {
            *(s for s, spec in self.specs.items() if self.proxy.visible(spec)),
            *gateways.NAMES,
        }

    async def call_tool(self, name, arguments) -> CallToolResult:
        arguments = arguments if isinstance(arguments, dict) else {}
        if self.proxy.connected_key() != self.proxy._key:
            await self.proxy.refresh()  # NX (re)connected: its build and session state
        if name in gateways.NAMES:
            answer = await self._gateway(name, arguments)
        else:
            answer = await self._run(
                name,
                arguments,
                intent=None,
                detail="compact" if self.profile == "agent" else "full",
            )
        error = (answer.structuredContent or {}).get("error") or {}
        if error.get("code") == "NX_LICENSE_UNAVAILABLE":
            details = error.get("details") or {}
            known = details.get("remembered_from") and evidence.remembered(
                self.proxy.licenses, details.get("tool"), details.get("variant")
            )
            if not known:
                await self.proxy.refresh(force=True)  # NX now remembers it
        await self.notify_if_changed()
        return answer

    async def notify_if_changed(self) -> None:
        """Send tools/list_changed when the listing build or session state changed
        since the client last listed the tools."""
        current = self.proxy.signature()
        if self._listed is None or current == self._listed:
            return
        self._listed = current
        with contextlib.suppress(Exception):
            session = self.mcp._mcp_server.request_context.session
            await session.send_tool_list_changed()
            logger.info("Tool listing changed (build %s); sent tools/list_changed", current[0])

    async def _run(self, name, arguments, *, intent, detail, direct=True) -> CallToolResult:
        spec = self.specs.get(name)
        if spec is not None and self.proxy.visible(spec):
            return await self.proxy.call(spec, arguments, intent=intent, detail=detail)
        args = arguments if isinstance(arguments, dict) else {}
        entry = self.proxy.remembered(spec, spec.variant_of(args)) if spec is not None else None
        if entry is not None:
            error = evidence.license_error(entry)
        else:
            error = self.unknown(name, field=None if direct else "/tool")
        return result(failure(str(name), error, "not_started"))

    async def _gateway(self, name, arguments) -> CallToolResult:
        try:
            request = gateways.MODELS[name].model_validate(arguments)
        except ValidationError as error:
            first = error.errors()[0]
            field = schema.error_pointer(arguments, first["loc"], first["type"]) or None
            problem = NXError(
                "NX_INVALID_ARGUMENT", f"{field or 'arguments'}: {first['msg']}", field=field
            )
            return result(failure(name, problem, "not_applicable"))
        if name in ("nx_find_tools", "nx_get_tool_schemas"):
            await self.proxy.refresh()
            return self._find(request) if name == "nx_find_tools" else self._schemas(request)
        request = cast("gateways.Invoke | gateways.InvokeRead", request)  # the other gateways
        target = request.tool
        if target in gateways.NAMES:
            problem = NXError(
                "NX_UNSUPPORTED_OPTION",
                f"{target} is a gateway; call it directly",
                field="/tool",
            )
            return result(failure(target, problem, "not_started"))
        if name == "nx_invoke_read" and self.effect_of(target) != "read":
            if self.effect_of(target) is None:
                unknown = self.unknown(target)
                return result(failure(target, unknown, "not_started"))
            problem = NXError(
                "NX_UNSUPPORTED_OPTION",
                f"{target} is not a read-only tool (effect {self.effect_of(target)})",
                field="/tool",
                hint="Call it with nx_invoke",
            )
            return result(failure(target, problem, "not_started"))
        return await self._run(
            target,
            request.arguments,
            intent=getattr(request, "intent", None),
            detail=request.detail,
            direct=False,
        )

    def suggest(self, name: str) -> list[str]:
        """Tools for an unknown name: a typo's spelling, then tools found by its words."""
        return gateways.suggestions(name, self.known(), self._visible_entries(), self.index)

    def unknown(self, name: str, *, field: str | None = "/tool") -> NXError:
        return gateways.unknown_tool(
            name, self.known(), field=field, entries=self._visible_entries(), index=self.index
        )

    def effect_of(self, name) -> str | None:
        spec = self.specs.get(name)
        if spec is not None and self.proxy.visible(spec):
            return spec.effect
        return None

    # -- gateways --------------------------------------------------------------

    def _visible_entries(self):
        return [e for e in self.entries if self.proxy.visible(self.specs[e.name])]

    def row(self, entry, matched=None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "name": entry.name,
            "summary": entry.summary,
            "effect": entry.effect,
            "domain": entry.domain,
        }
        if not self.proxy.tested(self.specs[entry.name]):
            row["tested"] = False
        if matched is not None:
            row["matched"] = list(matched)
        return row

    def build_note(self) -> dict[str, Any]:
        """The build listings follow, and whether it is assumed (NX not connected)."""
        build, assumed = self.proxy.listing()
        note: dict[str, Any] = {"build": build}
        if assumed:
            note["assumed"] = True
            note["note"] = (
                f"NX is not connected: tools listed for the default supported build {build}; "
                "the list may change when NX connects"
            )
        return note

    def _search(self, query: str, request) -> tuple[list, bool, list[str], str]:
        """``(hits, partial, spellings, searched)``. Query words no tool matches are replaced
        by their close spellings (``spellings``; ``searched``: the query then run)."""
        searched, spellings = self.index.respell(query)
        hits, partial = gateways.ranked(
            self._visible_entries(),
            searched,
            request.domain,
            request.effect,
            variants=lambda e: self.proxy.variants(self.specs[e.name]),
            index=self.index,
        )
        # Spellings are alternatives ("revolve remove"), so missing one is not partial.
        return hits, partial and not spellings, spellings, searched

    def _rows(self, hits, query: str, offset: int, limit: int) -> list[dict[str, Any]]:
        """Rows of a page of hits; a row that missed a query word lists the words it matched
        (none listed: it matched them all)."""
        count = len(gateways.query_words(query))
        return [
            self.row(h.entry, h.matched if len(h.matched) < count else None)
            for h in hits[offset:][:limit]
        ]

    @staticmethod
    def _notes(query: str, partial: bool, spellings: list[str]) -> dict[str, Any]:
        notes: dict[str, Any] = {}
        if partial:
            notes["partial"] = True  # no tool matched every word; rows say what they matched
        if spellings:
            notes["did_you_mean"] = spellings  # searched for words no tool matches
        count = len(gateways.query_words(query))
        if count > MAX_QUERY_WORDS or (partial and count > 2 and TASK_JOINS.search(query)):
            notes["hint"] = (
                "One task per query: for several tasks pass queries, e.g. "
                "['revolve profile', 'pattern circular', 'unite bodies']"
            )
        typed = {w for w, _ in gateways.query_words(query)}
        missing = [text for word, text in gateways.MISSING.items() if word in typed]
        if missing:
            notes["note"] = " ".join(missing)
        return notes

    def _find(self, request) -> CallToolResult:
        if request.queries is not None:
            return self._find_each(request)
        if not request.query.strip() and request.domain is None:
            return self._overview(request.effect)
        hits, partial, spellings, searched = self._search(request.query, request)
        limit = request.limit or FIND_LIMIT
        data: dict[str, Any] = {
            "tools": self._rows(hits, searched, request.offset, limit),
            "call_with": CALL_WITH,
        }
        data.update(self._notes(request.query, partial, spellings))
        if not hits:  # never just empty: the domains to browse instead
            data["domains"] = self._domains(request.effect)
            data["hint"] = (
                "No tool matches these words: try others, or pass a domain below with an "
                "empty query to list its tools"
            )
        data["listing"] = self.build_note()
        end = min(request.offset + limit, len(hits))
        return result(
            {
                "ok": True,
                "tool": "nx_find_tools",
                "mutation_outcome": "not_applicable",
                "data": data,
                "page": {
                    "offset": request.offset,
                    "total": len(hits),
                    "next_offset": end if end < len(hits) else None,
                },
            }
        )

    def _find_each(self, request) -> CallToolResult:
        """``queries``: one ranked search per query, ``QUERIES_LIMIT`` rows each by default."""
        limit = request.limit or QUERIES_LIMIT
        results = []
        for query in request.queries:
            hits, partial, spellings, searched = self._search(query, request)
            item: dict[str, Any] = {
                "query": query,
                "tools": self._rows(hits, searched, request.offset, limit),
                "total": len(hits),
            }
            item.update(self._notes(query, partial, spellings))
            results.append(item)
        data: dict[str, Any] = {"results": results, "call_with": CALL_WITH}
        if any(not item["total"] for item in results):
            data["hint"] = "Some queries match no tool: try other words, or an empty query"
        data["listing"] = self.build_note()
        return result(
            {
                "ok": True,
                "tool": "nx_find_tools",
                "mutation_outcome": "not_applicable",
                "data": data,
            }
        )

    def _domains(self, effect) -> list[dict[str, Any]]:
        """The domains with their tool counts and a few key tools each."""
        domains: dict[str, list] = {}
        for entry in self._visible_entries():
            if effect is None or entry.effect == effect:
                domains.setdefault(entry.domain, []).append(entry.name)
        rows = []
        for domain in sorted(domains):
            names = domains[domain]
            key = [n for n in gateways.KEY_TOOLS.get(domain, ()) if n in names]
            rows.append({"domain": domain, "tools": len(names), "key": key or sorted(names)[:3]})
        return rows

    def _overview(self, effect) -> CallToolResult:
        """An empty query: the domain overview."""
        return result(
            {
                "ok": True,
                "tool": "nx_find_tools",
                "mutation_outcome": "not_applicable",
                "data": {
                    "domains": self._domains(effect),
                    "hint": "Search with task words (e.g. 'revolve', 'sketch polyline'), or "
                    "pass domain with an empty query to list one domain's tools",
                    "call_with": CALL_WITH,
                    "listing": self.build_note(),
                },
            }
        )

    def describe(self, name: str, include_output: bool) -> dict[str, Any] | None:
        base, _, variant = name.partition("#")
        spec = self.specs.get(base)
        if spec is not None and self.proxy.visible(spec):
            # Call keys (operation_id) go inside nx_invoke's arguments; intent is
            # nx_invoke's own argument, so neither is repeated per tool here.
            offered = self.proxy.variants(spec)
            if variant:
                if variant in spec.variant_values() and variant not in offered:
                    return None  # reported as not available (_schemas)
                try:
                    input_schema = schema.variant_schema(spec, variant, call_keys=False)
                except KeyError:
                    return None
            else:
                input_schema = schema.narrow_variants(
                    schema.input_schema(spec, call_keys=False), spec, offered
                )
            out: dict[str, Any] = {
                "name": name,
                "description": spec.description,
                "effect": spec.effect,
                "input_schema": input_schema,
            }
            needs = schema.required_by(spec, offered) if not variant else {}
            if needs:  # the fields a variant needs; tool#variant lists them in required
                out["required_by"] = needs
            example = dict(spec.example)
            if spec.variant_of(example) not in (None, *offered):
                example = {}  # the example uses a variant this build does not offer
            if example and (not variant or spec.variant_of(example) == variant):
                out["example"] = example
            if spec.requires:
                out["requires"] = list(spec.requires)
            build = self.proxy.listing()[0]
            entry = spec.evidence.get(build) if build else None
            if entry is not None:
                # true: the tool, and every variant it has, is tested on this build.
                variants = entry.get("variants") or []
                complete = set(variants) >= set(spec.variant_values())
                out["evidence"] = {build: True if complete else variants}
                if isinstance(spec.variants, str) and not complete:
                    untested = [v for v in offered if v not in variants]
                    if untested:  # development mode offers them for evidence runs
                        out["untested_variants"] = untested
            else:
                out["tested"] = False
            if include_output:
                out["output_schema"] = schema.output_schema(spec)
            return out
        return None

    def _schemas(self, request) -> CallToolResult:
        tools, missing, unavailable = [], [], []
        for name in request.names:
            described = self.describe(name, request.include_output)
            if described is None:
                base, _, variant = name.partition("#")
                spec = self.specs.get(base)
                reason = None
                if spec is not None and (not variant or variant in spec.variant_values()):
                    reason = self.proxy.unavailable(spec, variant or None)
                if reason is not None:
                    unavailable.append(
                        {"name": name, "reason": f"not available on this NX build: {reason}"}
                    )
                    continue
                hint = self.suggest(base)
                if spec is not None and self.proxy.visible(spec) and variant:
                    hint = [f"{base}#{v}" for v in self.proxy.variants(spec)]
                missing.append({"name": name, "close_matches": hint})
            else:
                tools.append(described)
        data: dict[str, Any] = {"tools": tools}
        if missing:
            data["not_found"] = missing
        if unavailable:
            data["not_available"] = unavailable
        if self.proxy.listing()[1]:
            data["listing"] = self.build_note()
        if not tools and unavailable and not missing:
            problem = NXError(
                "NX_VERSION_UNSUPPORTED",
                "; ".join(f"{u['name']}: {u['reason']}" for u in unavailable),
                field="/names",
                hint="Find available tools with nx_find_tools",
                details={"not_available": unavailable},
            )
            return result(failure("nx_get_tool_schemas", problem, "not_applicable"))
        if not tools:
            problem = NXError(
                "NX_UNKNOWN_TOOL",
                "None of the names is a known tool",
                field="/names",
                hint="Find tools with nx_find_tools",
                details={"not_found": missing},
            )
            return result(failure("nx_get_tool_schemas", problem, "not_applicable"))
        return result(
            {
                "ok": True,
                "tool": "nx_get_tool_schemas",
                "mutation_outcome": "not_applicable",
                "data": data,
            }
        )


def profile_from_env(surface: str | None) -> str:
    profile = surface or os.environ.get("NX_MCP_SURFACE") or "agent"
    if profile not in PROFILES:
        raise ValueError("NX_MCP_SURFACE must be full or agent")
    return profile


def install(mcp, bridge, workspace, profile: str, *, build=None) -> Surface:
    """Replace ``mcp``'s tools/list and tools/call with the generated surface."""
    surface = Surface(mcp, bridge, workspace, profile, build=build)
    server = mcp._mcp_server
    plain_options = server.create_initialization_options

    def options(notification_options=None, experimental_capabilities=None):
        # Listings change when NX connects or reports a missing license.
        notification_options = notification_options or NotificationOptions(tools_changed=True)
        return plain_options(notification_options, experimental_capabilities)

    server.create_initialization_options = options
    mcp.call_tool = surface.call_tool
    mcp.list_tools = surface.list_tools
    mcp._mcp_server.call_tool(validate_input=False)(surface.call_tool)
    mcp._mcp_server.list_tools()(surface.list_tools)
    mcp._mcp_server.instructions = AGENT_INSTRUCTIONS if profile == "agent" else FULL_INSTRUCTIONS
    mcp.nx_surface = surface
    return surface

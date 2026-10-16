"""The MCP surface generated from the catalogue.

``install(mcp, bridge, workspace, profile)`` replaces the ``tools/list`` and
``tools/call`` handlers of the FastMCP server:

- ``agent`` (default): the four gateways plus the core tools of tool-surface
  2.1 that exist in the catalogue;
- ``full``: the gateways and every catalogue tool.

Catalogue tools are listed from their specs (schema, docstring, MCP annotations
from ``effect``) and dispatched through :class:`CatalogueProxy`.

Listings follow the connected NX build (gate 3, ``core/evidence.py``): tools and
variants with evidence there, and on an untested build those with evidence on any
build, marked untested (nx_find_tools: ``listing: {build, tested: false}`` and a note on
the first page; nx_get_tool_schemas: ``tested: false`` per entry). Tools tested nowhere,
those NX refused for a missing license this session and those that failed on this
build for a missing NXOpen member are left out of tools/list, nx_find_tools and
nx_get_tool_schemas. A tested build adds no marker. Before NX connects listings follow
the default supported build (``evidence.DEFAULT_BUILD``) and say so; when the listing
changes, the server sends ``tools/list_changed``.
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
from nx_mcp.sidecar import artifacts, gateways, proxy, schema
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
    "into the conversation. Lengths are in part units and angles in degrees; length and angle "
    "fields typed number or string also take '10 mm'/'90 deg' or an NX expression name or "
    "formula ('thickness', 'thickness/2'), which keeps extrude, revolve, hole, pattern, blend "
    "and chamfer values driven by the expression; [x, y, z] points and vectors take numbers only. "
    "Options match in any case or spacing ('Through All'). When two or more parts are loaded, "
    "add the target part's file name to the arguments of every call without refs, the listed "
    'core tools included: {"part": "housing", ...}; nx_list_parts lists them.'
)
# Every tool that can be called without references lists ``part`` in its schemas (the
# ref-less core tools in the agent listing, every such tool in nx_get_tool_schemas and the
# full listing): schema.PART_KEY, schema.takes_part_key.
PART_KEY = schema.PART_KEY
CALL_WITH = "nx_invoke; nx_invoke_read for effect read (not as direct MCP tools)"
# The agent profile also lists the core tools as MCP tools of their own (rows: core).
CALL_WITH_CORE = "nx_invoke; nx_invoke_read for reads; core:true rows are also MCP tools"
# nx_find_tools: tools NX does not offer on this build (no evidence, a missing license)
# that would rank this high on the page are named in ``not_available`` with the reason,
# so an agent learns why its tool is missing instead of assuming none exists.
NOT_AVAILABLE_RANK, NOT_AVAILABLE_ROWS = 3, 3
# nx_find_tools: default rows for one query and per query of ``queries``; a query with more
# words than this, or joining tasks (``,`` ``;`` ``+`` "and" "then"), earns a hint to split
# the tasks into ``queries`` (a long single task, "assembly constraints status", does not).
FIND_LIMIT, QUERIES_LIMIT, MAX_QUERY_WORDS = 10, 5, 5
# nx_find_tools on an untested NX build: said once per connection or listing change, on a
# first page (``listing.note``).
UNTESTED_NOTE = (
    "{build} is not tested with NX MCP: tools tested on other builds are offered; verify "
    "results (nx_check_model, measurements). NX_VERSION_UNSUPPORTED means this tool cannot "
    "run here: do not retry, pick another."
)
READ_ONLY_NOTE = (
    "{build} is not tested with NX MCP and agents may only read here ({reason}): only read "
    "and view tools are offered."
)
TASK_JOINS = re.compile(r"[,;+&]|\b(?:and|then|also)\b", re.IGNORECASE)
FULL_INSTRUCTIONS = (
    "Siemens NX integration. Runtime support varies by NX version; no general certification "
    "is claimed. Tools return {ok, mutation_outcome, data | error}. Every changing tool takes "
    "an optional operation_id (8-128 characters from A-Z a-z 0-9 _ -; omitted, the server "
    "makes one; reuse your own to retry safely) and intent (why, in the engineer's terms; "
    "shown in the NX panel). File paths are workspace-relative or absolute inside the "
    "workspace. Length and angle fields typed number or string also take '10 mm'/'90 deg' or "
    "an NX expression name or formula ('thickness/2'); [x, y, z] points take numbers only."
)


def tool_annotations(effect: str, name: str, label: str | None = None) -> ToolAnnotations:
    """MCP hints from a spec effect (surface 1.3).

    A hint equal to MCP's default (readOnlyHint false, destructiveHint true,
    idempotentHint false, openWorldHint true) is left out: a client reads the same
    meaning from its absence, and the listing is paid on every turn.
    """
    verb = name.split("_")[1] if name.count("_") >= 2 else ""
    read = effect == "read"
    return ToolAnnotations(
        title=label,
        readOnlyHint=True if read else None,
        # MCP: destructiveHint means something only for tools that are not read-only;
        # model and session changes keep the default (true).
        destructiveHint=False if not read and effect not in ("model", "session") else None,
        idempotentHint=True if read or verb == "set" else None,
        openWorldHint=False,
    )


takes_part_key = schema.takes_part_key


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
        self._noted: tuple | None = None  # proxy.signature() when the untested note was given
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
        input_schema = schema.narrow_variants(
            schema.input_schema(spec), spec, self.proxy.variants(spec)
        )
        schema.with_part_key(spec, input_schema)
        return Tool(
            name=spec.name,
            title=spec.label,
            description=spec.description,
            inputSchema=input_schema,
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
        token = proxy.CALLER.set(self.caller())  # who sends it (core/busy.py)
        try:
            return await self._call_tool(name, arguments)
        finally:
            proxy.CALLER.reset(token)

    def caller(self) -> str:
        """The caller id of the MCP request being handled (``proxy.caller_id``)."""
        try:
            context = self.mcp._mcp_server.request_context
        except LookupError:  # called outside an MCP request (tests, the runner's direct use)
            return proxy.PROCESS_ID
        return proxy.caller_id(context)

    async def _call_tool(self, name, arguments) -> CallToolResult:
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
        details = error.get("details") or {}
        if error.get("code") == "NX_LICENSE_UNAVAILABLE":
            known = details.get("remembered_from") and evidence.remembered(
                self.proxy.licenses, details.get("tool"), details.get("variant")
            )
            if not known:
                await self.proxy.refresh(force=True)  # NX now remembers it
        elif error.get("code") == "NX_VERSION_UNSUPPORTED" and details.get("member"):
            known = evidence.remembered(
                self.proxy.versions, details.get("tool"), details.get("variant")
            )
            if not known:
                await self.proxy.refresh(force=True)  # NX remembers the missing member
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
        failed = self.proxy.failed(spec, spec.variant_of(args)) if spec is not None else None
        if entry is not None:
            error = evidence.license_error(entry)
        elif failed is not None:
            error = evidence.version_error(failed)
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

    def _hidden_entries(self):
        return [e for e in self.entries if not self.proxy.visible(self.specs[e.name])]

    def call_with(self) -> str:
        return CALL_WITH_CORE if self.profile == "agent" else CALL_WITH

    def row(self, entry, matched=None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "name": entry.name,
            "summary": entry.summary,
            "effect": entry.effect,
            "domain": entry.domain,
        }
        if self.profile == "agent" and entry.name in CORE_TOOLS:
            row["core"] = True  # also listed as an MCP tool of its own
        # Untested here; on an untested build the page says it once (``listing``).
        if self.proxy.build_tested() and not self.proxy.tested(self.specs[entry.name]):
            row["tested"] = False
        if matched is not None:
            row["matched"] = list(matched)
        return row

    def build_note(self, first: bool = True) -> dict[str, Any] | None:
        """The build listings follow, when it needs saying: assumed (NX not connected) or
        untested (``tested: false``, with :data:`UNTESTED_NOTE` on a ``first`` page once
        per connection or listing change); None on a tested, connected build."""
        build, assumed = self.proxy.listing()
        note: dict[str, Any] = {"build": build}
        if assumed:
            note["assumed"] = True
            note["note"] = (
                f"NX is not connected: tools listed for the default supported build {build}; "
                "the list may change when NX connects"
            )
            return note
        if self.proxy.build_tested():
            return None
        note["tested"] = False
        signature = self.proxy.signature()
        if first and self._noted != signature:
            self._noted = signature
            shown = evidence.build_label(self.proxy.nx_build())
            if self.proxy.read_only():
                reason = self.proxy.read_only_reason or "NX_MCP_UNTESTED=read_only"
                note["note"] = READ_ONLY_NOTE.format(build=shown, reason=reason)
            else:
                note["note"] = UNTESTED_NOTE.format(build=shown)
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
            fuzzy=spellings,
        )
        # Spellings are alternatives ("revolve remove"), so missing one is not partial.
        return hits, partial and not spellings, spellings, searched

    def _not_available(self, hits, searched: str, spellings, request) -> list[dict[str, Any]]:
        """Tools this build does not offer that would rank among the first
        NOT_AVAILABLE_RANK rows of ``hits``, with the reason (at most NOT_AVAILABLE_ROWS)."""
        hidden = self._hidden_entries()
        if not hidden or not gateways.query_words(searched):
            return []
        found, _ = gateways.ranked(
            hidden, searched, request.domain, request.effect, index=self.index, fuzzy=spellings
        )
        bar = hits[NOT_AVAILABLE_RANK - 1].score if len(hits) >= NOT_AVAILABLE_RANK else 0.0
        rows = []
        for hit in found[:NOT_AVAILABLE_ROWS]:
            if hit.score < bar:
                break
            reason = self.proxy.unavailable(self.specs[hit.entry.name])
            if reason is not None:
                rows.append({"name": hit.entry.name, "summary": hit.entry.summary,
                             "reason": reason})  # fmt: skip
        return rows

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
            "call_with": self.call_with(),
        }
        hidden = self._not_available(hits, searched, spellings, request)
        if hidden and not request.offset:
            data["not_available"] = hidden
        data.update(self._notes(request.query, partial, spellings))
        if not hits:  # never just empty: the domains to browse instead
            data["domains"] = self._domains(request.effect)
            data["hint"] = (
                "No tool matches these words: try others, or pass a domain below with an "
                "empty query to list its tools"
            )
        listing = self.build_note(first=not request.offset)
        if listing is not None:
            data["listing"] = listing
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
            hidden = self._not_available(hits, searched, spellings, request)
            if hidden and not request.offset:
                item["not_available"] = hidden
            item.update(self._notes(query, partial, spellings))
            results.append(item)
        data: dict[str, Any] = {"results": results, "call_with": self.call_with()}
        if any(not item["total"] for item in results):
            data["hint"] = "Some queries match no tool: try other words, or an empty query"
        listing = self.build_note(first=not request.offset)
        if listing is not None:
            data["listing"] = listing
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
        data: dict[str, Any] = {
            "domains": self._domains(effect),
            "hint": "Search with task words (e.g. 'revolve', 'sketch polyline'), or "
            "pass domain with an empty query to list one domain's tools",
            "call_with": self.call_with(),
        }
        listing = self.build_note()
        if listing is not None:
            data["listing"] = listing
        return result(
            {
                "ok": True,
                "tool": "nx_find_tools",
                "mutation_outcome": "not_applicable",
                "data": data,
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
            schema.with_part_key(spec, input_schema)
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
            # Tested on this build adds nothing; else ``tested: false`` (the tool) or
            # ``untested_variants`` (offered variants tested on other builds only, or
            # untested ones development mode offers for evidence runs).
            build = self.proxy.listing()[0]
            entry = spec.evidence.get(build) if build else None
            if entry is not None:
                variants = entry.get("variants") or []
                if isinstance(spec.variants, str):
                    untested = [v for v in offered if v not in variants]
                    if untested:
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
        data: dict[str, Any] = {"tools": group_variants(tools)}
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


def group_variants(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``nx_get_tool_schemas`` entries with two or more variants of one tool (``tool#a``,
    ``tool#b``) merged into one entry at the first one's place (tool-surface 2.1):

    ``{"name": tool, <keys every variant shares: description, effect, requires...>,
    "input_schema": <what every variant's schema shares>, "variants": {"a": {"input_schema":
    <a's own properties, required and other keys>, <a's own keys: example...>}, ...}}``

    The schema of ``tool#a`` is ``input_schema`` with ``variants.a.input_schema``'s
    ``properties`` added, its ``required`` appended and its other keys set. A single
    ``tool#variant`` stays one self-contained entry.
    """
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in entries:
        base, _, variant = entry["name"].partition("#")
        if variant:
            groups.setdefault(base, {}).setdefault(entry["name"], entry)
    out: list[dict[str, Any]] = []
    merged: set[str] = set()
    for entry in entries:
        base = entry["name"].partition("#")[0]
        group = groups.get(base, {}) if "#" in entry["name"] else {}
        if len(group) < 2:
            out.append(entry)
        elif base not in merged:
            merged.add(base)
            out.append(_merge_variants(base, list(group.values())))
    return out


def _merge_variants(base: str, group: list[dict[str, Any]]) -> dict[str, Any]:
    schemas = [g["input_schema"] for g in group]
    first, rest = schemas[0], schemas[1:]
    props = {
        k: v
        for k, v in first.get("properties", {}).items()
        if all(s.get("properties", {}).get(k, ...) == v for s in rest)
    }
    needed = [r for r in first.get("required", []) if all(r in s.get("required", ()) for s in rest)]
    shared: dict[str, Any] = {}
    for key, value in first.items():
        if key == "properties":
            shared[key] = props
        elif key == "required":
            if needed:
                shared[key] = needed
        elif all(s.get(key, ...) == value for s in rest):
            shared[key] = value
    entry: dict[str, Any] = {"name": base}
    for key, value in group[0].items():
        if key == "input_schema":
            entry[key] = shared
            entry["variants"] = {}
        elif key != "name" and all(g.get(key, ...) == value for g in group[1:]):
            entry[key] = value
    for item, own_schema in zip(group, schemas, strict=True):
        own: dict[str, Any] = {}
        extra = {k: v for k, v in own_schema.get("properties", {}).items() if k not in props}
        if extra:
            own["properties"] = extra
        more = [r for r in own_schema.get("required", []) if r not in needed]
        if more:
            own["required"] = more
        own.update(
            {k: v for k, v in own_schema.items() if k not in ("properties", "required", *shared)}
        )
        variant: dict[str, Any] = {"input_schema": own}
        variant.update({k: v for k, v in item.items() if k not in ("name", *entry)})
        entry["variants"][item["name"].partition("#")[2]] = variant
    return entry


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

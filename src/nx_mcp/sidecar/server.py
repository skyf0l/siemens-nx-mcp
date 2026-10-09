"""The MCP surface generated from the catalogue.

``install(mcp, bridge, workspace, profile)`` replaces the ``tools/list`` and
``tools/call`` handlers of the FastMCP server:

- ``agent`` (default): the four gateways plus the core tools of tool-surface
  2.1 that exist in the catalogue;
- ``full``: the gateways and every catalogue tool.

Catalogue tools are listed from their specs (schema, docstring, MCP annotations
from ``effect``) and dispatched through :class:`CatalogueProxy`.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.types import CallToolResult, Tool, ToolAnnotations
from pydantic import ValidationError

from nx_mcp.core import catalogue
from nx_mcp.core.catalogue import CatalogueError
from nx_mcp.core.errors import NXError
from nx_mcp.sidecar import artifacts, gateways, schema
from nx_mcp.sidecar.proxy import CatalogueProxy, failure, result

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
    "NX agent profile. Find tools with nx_find_tools (task words), fetch exact schemas with "
    "nx_get_tool_schemas (1-10 names; 'tool#variant' for one variant), then call nx_invoke, or "
    "nx_invoke_read for read-only tools. Results carry ok, mutation_outcome and data, or error "
    "{category, code, retry, hint, field}. Put operation_id inside arguments for mutations you "
    "may retry; after an unknown outcome query nx_get_operation before retrying. "
    "Compact results keep result_id; "
    "expand the omitted fields with nx_get_result. Never paste base64 file content into the conversation."
)
FULL_INSTRUCTIONS = (
    "Siemens NX integration. Runtime support varies by NX version; no general certification "
    "is claimed. Tools return {ok, mutation_outcome, data | error}. Every changing tool takes "
    "operation_id (reuse it to retry safely) and intent (why, in the engineer's terms; shown "
    "in the NX panel). File paths are workspace-relative or absolute inside the workspace."
)


def annotations(effect: str, name: str, label: str | None = None) -> ToolAnnotations:
    """MCP hints from a spec effect (surface 1.3)."""
    verb = name.split("_")[1] if name.count("_") >= 2 else ""
    return ToolAnnotations(
        title=label,
        readOnlyHint=effect == "read",
        destructiveHint=effect in ("model", "session"),
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

    # -- listing ---------------------------------------------------------------

    def gateway_tools(self) -> list[Tool]:
        effects = {"nx_invoke": "model"}
        return [
            Tool(
                name=name,
                description=gateways.DESCRIPTIONS[name],
                inputSchema=gateways.tool_schema(name),
                annotations=annotations(effects.get(name, "read"), name),
            )
            for name in gateways.NAMES
        ]

    def catalogue_tool(self, spec) -> Tool:
        return Tool(
            name=spec.name,
            title=spec.label,
            description=spec.description,
            inputSchema=schema.input_schema(spec),
            annotations=annotations(spec.effect, spec.name),
        )

    async def list_tools(self) -> list[Tool]:
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
        if name in gateways.NAMES:
            return await self._gateway(name, arguments)
        return await self._run(
            name, arguments, intent=None, detail="compact" if self.profile == "agent" else "full"
        )

    async def _run(self, name, arguments, *, intent, detail, direct=True) -> CallToolResult:
        spec = self.specs.get(name)
        if spec is not None and self.proxy.visible(spec):
            return await self.proxy.call(spec, arguments, intent=intent, detail=detail)
        error = gateways.unknown_tool(name, self.known(), field=None if direct else "/tool")
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
        if name == "nx_find_tools":
            return self._find(request)
        if name == "nx_get_tool_schemas":
            return self._schemas(request)
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
                error = gateways.unknown_tool(target, self.known())
                return result(failure(target, error, "not_started"))
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

    def effect_of(self, name) -> str | None:
        spec = self.specs.get(name)
        if spec is not None and self.proxy.visible(spec):
            return spec.effect
        return None

    # -- gateways --------------------------------------------------------------

    def _visible_entries(self):
        return [e for e in self.entries if self.proxy.visible(self.specs[e.name])]

    def row(self, entry) -> dict[str, Any]:
        row: dict[str, Any] = {
            "name": entry.name,
            "summary": entry.summary,
            "effect": entry.effect,
            "domain": entry.domain,
        }
        if not self.proxy.tested(self.specs[entry.name]):
            row["tested"] = False
        return row

    def _find(self, request) -> CallToolResult:
        found = gateways.search(
            self._visible_entries(), request.query, request.domain, request.effect
        )
        selected = found[request.offset : request.offset + request.limit]
        end = request.offset + len(selected)
        return result(
            {
                "ok": True,
                "tool": "nx_find_tools",
                "mutation_outcome": "not_applicable",
                "data": {"tools": [self.row(e) for e in selected]},
                "page": {
                    "offset": request.offset,
                    "total": len(found),
                    "next_offset": end if end < len(found) else None,
                },
            }
        )

    def describe(self, name: str, include_output: bool) -> dict[str, Any] | None:
        base, _, variant = name.partition("#")
        spec = self.specs.get(base)
        if spec is not None and self.proxy.visible(spec):
            # Call keys (operation_id) go inside nx_invoke's arguments; intent is
            # nx_invoke's own argument, so neither is repeated per tool here.
            if variant:
                try:
                    input_schema = schema.variant_schema(spec, variant, call_keys=False)
                except KeyError:
                    return None
            else:
                input_schema = schema.input_schema(spec, call_keys=False)
            out: dict[str, Any] = {
                "name": name,
                "description": spec.description,
                "effect": spec.effect,
                "input_schema": input_schema,
            }
            example = dict(spec.example)
            if not variant or spec.variant_of(example) == variant:
                out["example"] = example
            if spec.requires:
                out["requires"] = list(spec.requires)
            build = self.proxy.build()
            entry = spec.evidence.get(build) if build else None
            if entry is not None:
                # true: the tool, and every variant it has, is tested on this build.
                variants = entry.get("variants") or []
                complete = set(variants) >= set(spec.variant_values())
                out["evidence"] = {build: True if complete else variants}
            else:
                out["tested"] = False
            if include_output:
                out["output_schema"] = schema.output_schema(spec)
            return out
        return None

    def _schemas(self, request) -> CallToolResult:
        tools, missing = [], []
        for name in request.names:
            described = self.describe(name, request.include_output)
            if described is None:
                base = name.partition("#")[0]
                hint = gateways.close_matches(base, self.known())
                if base in self.specs and "#" in name:
                    hint = [f"{base}#{v}" for v in self.specs[base].variant_values()]
                missing.append({"name": name, "close_matches": hint})
            else:
                tools.append(described)
        data: dict[str, Any] = {"tools": tools}
        if missing:
            data["not_found"] = missing
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
    mcp.call_tool = surface.call_tool
    mcp.list_tools = surface.list_tools
    mcp._mcp_server.call_tool(validate_input=False)(surface.call_tool)
    mcp._mcp_server.list_tools()(surface.list_tools)
    mcp._mcp_server.instructions = AGENT_INSTRUCTIONS if profile == "agent" else FULL_INSTRUCTIONS
    mcp.nx_surface = surface
    return surface

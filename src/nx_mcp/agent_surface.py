"""Legacy agent presentation: compact results, snapshots, artifacts and helper tools.

Tool listing, discovery and the gateways moved to :mod:`nx_mcp.sidecar` (step F3).
What stays here serves legacy tools until their domains migrate (deleted in F4):

- ``compact``/``compact_payload``: the bounded projection of a legacy result;
- :class:`ResultStore`: durable response snapshots (``result_id``);
- the ``nx-artifact://`` resource template for workspace artifacts;
- the helper tools ``nx_result``, ``nx_result_cleanup`` and ``nx_inspect``.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, unquote

from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import ToolManager
from mcp.types import CallToolResult, ResourceLink, TextContent, Tool, ToolAnnotations

from nx_mcp.agent_guidance import next_actions
from nx_mcp.result_retention import LOCK, maintain, settings

DOMAINS = {
    "sketch": ("sketch", "constraint"),
    "assembly": ("component", "assembly", "explosion", "reference_set", "mate"),
    "drawing": (
        "drawing",
        "balloon",
        "bom",
        "pmi",
        "annotation",
        "dimension",
        "note",
        "center_mark",
        "centerline",
        "surface_finish",
        "weld",
    ),
    "manufacturing": ("sheet_metal", "flat_pattern", "thread", "draft_analysis", "thickness"),
    "inspection": ("measure", "bounding", "interference", "collision", "topology", "diagnostic"),
    "display": (
        "view",
        "display",
        "visibility",
        "section",
        "render",
        "screenshot",
        "datum_visibility",
    ),
    "files": (
        "part",
        "workspace",
        "directory",
        "upload",
        "download",
        "export",
        "import",
        "package",
    ),
}
DEFAULTS: dict[str, dict[str, Any]] = {
    "nx_list_topology": {"compact": True},
}
# Helper tools served by the sidecar itself; read helpers never change anything.
HELPERS = {"nx_result": "read", "nx_inspect": "read", "nx_result_cleanup": "file"}


def category(name):
    return next(
        (key for key, words in DOMAINS.items() if any(w in name for w in words)), "modeling"
    )


def compact(value, path="", omitted=None, paged_keys=()):
    """Preserve scalars, coordinate vectors, identities and all warnings; bound other arrays."""
    omitted = {} if omitted is None else omitted
    if isinstance(value, dict):
        if "id" in value and "kind" in value:
            return {
                k: v
                for k, v in value.items()
                if k in {"id", "kind", "name", "part_id", "occurrence_path"}
            }
        result = {}
        for key, item in value.items():
            location = f"{path}/{key}"
            if key == "data_base64":
                omitted[location] = "binary omitted; use resources/read or a programmatic download"
            elif key in {"warnings", "retry_guidance", "recovery"}:
                result[key] = item
            else:
                result[key] = (
                    [compact(row, f"{location}/{i}", omitted) for i, row in enumerate(item)]
                    if key in paged_keys and isinstance(item, list)
                    else compact(item, location, omitted)
                )
        return result
    if isinstance(value, list):
        if len(value) > 20:
            omitted[path] = {"total_count": len(value), "returned_count": 20}
        return [compact(item, f"{path}/{i}", omitted) for i, item in enumerate(value[:20])]
    return value


def compact_payload(full, result_id, method=None):
    if full.get("status") == "error":
        return full
    omitted: dict[str, Any] = {}
    # A backend page is already bounded by its requested limit. Never truncate its
    # rows while retaining the backend count/cursor: that silently skips entries.
    paged_keys = ("parts", "components", "entries", "items") if "next_offset" in full else ()
    payload = compact(full, omitted=omitted, paged_keys=paged_keys)
    payload.update(result_id=result_id, detail="compact")
    if omitted:
        payload["omitted"] = omitted
    changes = full.get("changes")
    if isinstance(changes, dict):
        payload["change_counts"] = {
            k: len(v) if isinstance(v, list) else None
            for k, v in changes.items()
            if k in {"created", "modified", "deleted"}
        }
    if actions := next_actions(method, full):
        payload["next_actions"] = actions
    return payload


class ResultStore:
    """Durable inspection snapshots, separate from authoritative mutation receipts."""

    def __init__(self, root):
        self.root = Path(root) / ".nx-mcp" / "agent-results"
        settings()  # Validate configuration before any mutation can be dispatched.

    def put(self, payload):
        with LOCK:
            return self._put(payload)

    def _put(self, payload):
        if len(json.dumps(payload).encode("utf-8")) > settings()["max_bytes"]:
            raise OSError("Response exceeds snapshot storage limit")
        self.root.mkdir(parents=True, exist_ok=True)
        result_id = "result_" + uuid.uuid4().hex
        path = self.root / (result_id + ".json")
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        maintain(self.root, apply=True, protect=result_id)
        return result_id

    def get(self, result_id):
        if not re.fullmatch(r"result_[a-f0-9]{32}", result_id):
            raise ValueError("Invalid result_id")
        return json.loads((self.root / (result_id + ".json")).read_text(encoding="utf-8"))


class Helpers:
    """Presentation and helper tools for legacy results (see :func:`install`).

    The helper tools live in their own tool manager, so the legacy registry (and
    the release scripts that count it) stays exactly the manifest's tools.
    """

    names = frozenset(HELPERS)

    def __init__(self, workspace, store):
        self.manager = ToolManager(warn_on_duplicate_tools=True)
        self.workspace = workspace
        self.store = store

    def tool(self, annotations):
        def register(fn):
            self.manager.add_tool(fn, name=fn.__name__, annotations=annotations)
            return fn

        return register

    def tools(self):
        """The helper tools (FastMCP ``Tool`` objects) by name."""
        return {name: self.manager.get_tool(name) for name in HELPERS}

    def listed(self) -> list[Tool]:
        """MCP definitions of the helper tools for the ``full`` listing."""
        return [
            Tool(
                name=t.name,
                description=t.description,
                inputSchema=t.parameters,
                annotations=t.annotations,
            )
            for t in self.tools().values()
        ]

    def present(self, response, mode, method=None):
        """Compact a legacy result; never turns a finished call into an error."""
        if not isinstance(response, CallToolResult) or mode == "full" or response.isError:
            return response
        full = response.structuredContent
        if not isinstance(full, dict):
            return response
        from nx_mcp.integration_server import envelope

        try:
            payload = compact_payload(full, self.store.put(full), method)
            result = envelope(payload)
            result.content.extend(c for c in response.content if not isinstance(c, TextContent))
            if full.get("path") and full.get("sha256") and full.get("size", 0) <= 8 * 1024 * 1024:
                file = self.workspace.resolve(full["path"])
                relative = file.relative_to(self.workspace.root).as_posix()
                result.content.append(
                    ResourceLink(
                        type="resource_link",
                        name=file.name,
                        uri="nx-artifact://workspace/" + quote(relative, safe=""),
                        size=full.get("size"),
                    )
                )
        except Exception:
            # The call already ran in NX: a presentation fault (snapshot storage,
            # artifact link) must not relabel its outcome. Return it uncompacted.
            return response
        return result

    async def call(self, name, arguments):
        """Run a helper tool; its own failures happen before or without any NX change."""
        from nx_mcp.integration_server import envelope
        from nx_mcp.runtime import NXToolError

        try:
            return await self.manager.call_tool(name, arguments or {}, convert_result=False)
        except (ValueError, KeyError, IndexError, OSError, TypeError, ToolError) as error:
            expired = isinstance(error, FileNotFoundError) or isinstance(
                error.__cause__, FileNotFoundError
            )
            outcome = "not_applicable" if HELPERS[name] == "read" else "not_started"
            return envelope(
                NXToolError(
                    "NX_RESULT_EXPIRED" if expired else "NX_INVALID_ARGUMENT",
                    str(error),
                    details={"mutation_outcome": outcome},
                ).as_dict(),
                True,
            )


def install(mcp, workspace, legacy_call) -> Helpers:
    """Register the artifact resource and the helper tools on ``mcp``."""
    from nx_mcp.integration_server import envelope

    store = ResultStore(workspace.root)
    helpers = Helpers(workspace, store)

    @mcp.resource("nx-artifact://workspace/{path}", mime_type="application/octet-stream")
    def artifact(path: str) -> bytes:
        """Read a workspace artifact (8 MiB limit); keep binary content outside model text."""
        file = workspace.resolve(unquote(path))
        if ".nx-mcp" in {p.casefold() for p in file.relative_to(workspace.root).parts}:
            raise ValueError("Internal service state is not an artifact")
        if file.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Resource exceeds 8 MiB; use programmatic nx_download_file chunks")
        return file.read_bytes()

    @helpers.tool(ToolAnnotations(readOnlyHint=True))
    async def nx_result(
        result_id: str,
        field: str = "",
        offset: int = 0,
        limit: int = 20,
        detail: Literal["compact", "full"] = "compact",
    ) -> CallToolResult:
        """Expand a retained response snapshot without repeating NX operations. field is a JSON Pointer (/bodies, /changes/created); arrays are paged with limit 1..100. Empty field returns compact data plus available top-level fields. detail=full returns full selected object/scalar data; arrays remain paged. Snapshots survive server restarts until workspace cleanup; references may be stale after model changes. Use nx_get_operation for authoritative mutation recovery."""
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset >= 0; limit 1..100")
        value = store.get(result_id)
        if field:
            if not field.startswith("/"):
                raise ValueError("field must be a JSON Pointer")
            for token in field[1:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                value = value[int(token)] if isinstance(value, list) else value[token]
        if isinstance(value, list):
            return envelope(
                {
                    "result_id": result_id,
                    "field": field,
                    "items": value[offset : offset + limit],
                    "total_count": len(value),
                    "next_offset": offset + limit if offset + limit < len(value) else None,
                }
            )
        omitted: dict[str, Any] = {}
        payload = compact(value, omitted=omitted) if detail == "compact" else value
        return envelope(
            {
                "result_id": result_id,
                "field": field,
                "value": payload,
                "fields": list(value) if isinstance(value, dict) else [],
                "omitted": omitted,
            }
        )

    @helpers.tool(ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def nx_result_cleanup(
        dry_run: bool = True,
        max_age_seconds: int | None = None,
        max_bytes: int | None = None,
    ) -> CallToolResult:
        """Inspect or delete disposable response snapshots. Default dry_run=true previews eligible counts/bytes. Limits are positive integers; default retention is seven days/256 MiB, configurable with NX_MCP_RESULT_MAX_AGE_SECONDS and NX_MCP_RESULT_MAX_BYTES. Automatic pruning runs on snapshot writes. Mutation recovery receipts and CAD files are never included. Call with dry_run=false to apply; old result_ids may then expire."""
        return envelope(
            maintain(
                store.root, apply=not dry_run, max_age_seconds=max_age_seconds, max_bytes=max_bytes
            )
        )

    @helpers.tool(ToolAnnotations(readOnlyHint=True))
    async def nx_inspect(
        tool: Literal[
            "nx_list_features", "nx_list_sketches", "nx_list_topology", "nx_list_annotations"
        ],
        arguments: dict[str, Any] | None = None,
        collection: Literal["objects", "faces", "edges", "items"] | None = None,
        name_contains: str = "",
        kind: str | None = None,
        offset: int = 0,
        limit: int = 20,
        result_id: str | None = None,
    ) -> CallToolResult:
        """Filter and page features, sketches, topology or annotations. Filter by case-insensitive name/text and exact object kind before pagination. Topology requires arguments.body and collection=faces or edges. Returns total_count, count, next_offset, units and reported coordinate frame. Reuse result_id to page the same snapshot without another NX call; omit it to refresh after edits. Snapshot references can expire. Only the four named read-only tools are accepted."""
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset >= 0; limit 1..100")
        allowed = {
            "nx_list_features": {"objects"},
            "nx_list_sketches": {"objects"},
            "nx_list_topology": {"faces", "edges"},
            "nx_list_annotations": {"items"},
        }
        collection = collection or (
            "faces"
            if tool == "nx_list_topology"
            else ("items" if tool == "nx_list_annotations" else "objects")
        )
        if collection not in allowed[tool]:
            raise ValueError("Unsupported collection for this tool")
        if result_id:
            if arguments:
                raise ValueError(
                    "arguments cannot be combined with result_id; omit result_id to refresh"
                )
            snapshot = store.get(result_id)
            if snapshot.get("inspection_tool") != tool:
                raise ValueError("Snapshot belongs to a different inspection tool")
            full = snapshot["result"]
        else:
            arguments = arguments or {}
            if tool == "nx_list_annotations" and ({"offset", "limit"} & arguments.keys()):
                raise ValueError(
                    "Use nx_inspect offset/limit, not underlying annotation pagination"
                )
            response = await legacy_call(tool, arguments)
            if response.isError:
                return response
            full = response.structuredContent
            if tool == "nx_list_annotations":
                while full.get("next_offset") is not None:
                    response = await legacy_call(
                        tool, {"offset": full["next_offset"], "limit": 100}
                    )
                    if response.isError:
                        return response
                    more = response.structuredContent
                    if more.get("total") != full.get("total") or more.get(
                        "next_offset"
                    ) == full.get("next_offset"):
                        raise ValueError(
                            "Annotation inventory changed during capture; refresh inspection"
                        )
                    full["items"].extend(more["items"])
                    full["next_offset"] = more.get("next_offset")
            result_id = store.put({"inspection_tool": tool, "result": full})
        rows = full[collection]

        def matches(row):
            ref = row.get("object", row)
            text = str(ref.get("name", "")) + " " + str(row.get("text", ""))
            return name_contains.casefold() in text.casefold() and (
                kind is None or ref.get("kind") == kind
            )

        selected = [row for row in rows if matches(row)]
        items = selected[offset : offset + limit]
        omitted: dict[str, Any] = {}
        rendered = [compact(row, f"/items/{i}", omitted) for i, row in enumerate(items)]
        return envelope(
            {
                "result_id": result_id,
                "tool": tool,
                "collection": collection,
                "items": rendered,
                "omitted": omitted,
                "count": len(items),
                "total_count": len(selected),
                "unfiltered_count": len(rows),
                "offset": offset,
                "next_offset": offset + len(items) if offset + len(items) < len(selected) else None,
                "units": full.get("units"),
                "coordinate_frame": full.get(
                    "coordinate_frame", full.get("position_frame", "not_reported")
                ),
                "warnings": full.get("warnings", []),
            }
        )

    for tool in helpers.tools().values():
        tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
        tool.fn_metadata.arg_model.model_rebuild(force=True)
        tool.parameters = tool.fn_metadata.arg_model.model_json_schema()
    return helpers

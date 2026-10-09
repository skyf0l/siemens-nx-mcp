"""Legacy agent presentation: compact results, snapshots and artifacts.

Tool listing, discovery and the gateways moved to :mod:`nx_mcp.sidecar` (step F3).
What stays here serves legacy tools until their domains migrate (deleted in F4):

- ``compact``/``compact_payload``: the bounded projection of a legacy result;
- :class:`ResultStore`: durable response snapshots (``result_id``);
- the ``nx-artifact://`` resource template for workspace artifacts.

Snapshots are read and pruned by ``nx_get_result`` and ``nx_delete_results``
(``domains/session/results.py``).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from mcp.types import CallToolResult, ResourceLink, TextContent

from nx_mcp.agent_guidance import next_actions
from nx_mcp.kit.snapshots import LOCK, maintain, settings

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
DEFAULTS: dict[str, dict[str, Any]] = {}


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
        payload["expand_with"] = "nx_get_result"
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
    """Compact presentation of legacy results over the snapshot store (see :func:`install`)."""

    def __init__(self, workspace, store):
        self.workspace = workspace
        self.store = store

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


def install(mcp, workspace) -> Helpers:
    """Register the artifact resource on ``mcp``; returns the legacy presenter."""
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

    return helpers

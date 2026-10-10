"""Stored result snapshots (tool-surface 3.1): read and prune them.

A compact result (``detail=compact``) cuts arrays over 20 items and names the
snapshot that holds the whole result: ``result_id`` plus ``omitted`` (JSON
pointer -> counts) and ``expand_with: nx_get_result``. Bridge results too large
for the bridge (``result_transport.bound_result``) are kept the same way. Both
live under ``.nx-mcp`` in the workspace and survive restarts until pruned.

``nx_get_result`` (replaces ``nx_read_result``, ``nx_result`` and ``nx_inspect``)
pages or expands one snapshot by JSON pointer; ``nx_delete_results`` (replaces
``nx_result_cleanup``) previews or prunes them. Both run in the sidecar
(``runs="sidecar"``) and never reach NX; mutation receipts and CAD files are
never touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Offset, Range, Text
from nx_mcp.kit import result
from nx_mcp.kit.snapshots import LOCK, maintain, settings

# The two snapshot folders under <workspace>/.nx-mcp: sidecar compaction, bridge overflow.
STORES = ("agent-results", "bridge-results")
COMPACT_ITEMS = 20
COMPACT_CHARS = 2048
MAX_RESPONSE = 400 * 1024
ID_PATTERN = r"^result_[0-9a-f]{32}$"

ResultId = Annotated[str, Text(39, ID_PATTERN), Doc("result_id of a compact or oversized result")]
Pointer = Annotated[
    str, Text(512, r"^(/.*)?$"), Doc("JSON pointer into the result, e.g. /data/items/3")
]
Limit = Annotated[int, Range(ge=1, le=100), Doc("Rows per page")]
Detail = Annotated[
    Literal["compact", "full"], Doc("compact cuts nested arrays to 20 items; full returns all")
]


class ResultData(TypedDict, total=False):
    result_id: str
    field: str
    kind: str  # list (items), object (value: a page of its keys), text or value
    items: list[Any]
    value: Any
    omitted: dict  # pointer -> {total, returned}; read that pointer with nx_get_result
    page: dict


class CleanupData(TypedDict):
    applied: bool
    policy: dict  # {max_age_seconds, max_bytes}
    snapshot_count: int
    bytes_before: int
    selected_count: int
    bytes_after_cleanup: int
    missing: list[str]  # requested result_ids that do not exist


def directories(root) -> list[Path]:
    return [Path(root) / ".nx-mcp" / name for name in STORES]


def _file(root, result_id: str) -> Path | None:
    for folder in directories(root):
        path = folder / (result_id + ".json")
        if path.is_file() and not path.is_symlink():
            return path
    return None


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def compact(value, path: str, omitted: dict):
    """Arrays over 20 items and strings over 2048 characters are cut and listed in
    ``omitted`` by pointer; references and scalars stay whole."""
    if isinstance(value, dict):
        return {k: compact(v, f"{path}/{_escape(str(k))}", omitted) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > COMPACT_ITEMS:
            omitted[path] = {"total": len(value), "returned": COMPACT_ITEMS}
        return [compact(v, f"{path}/{i}", omitted) for i, v in enumerate(value[:COMPACT_ITEMS])]
    if isinstance(value, str) and len(value) > COMPACT_CHARS:
        omitted[path] = {"total": len(value), "returned": COMPACT_CHARS}
        return value[:COMPACT_CHARS]
    return value


def _select(value, field: str):
    for token in field.split("/")[1:]:
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not key.isdigit() or int(key) >= len(value):
                raise KeyError(key)
            value = value[int(key)]
        elif isinstance(value, dict) and key in value:
            value = value[key]
        else:
            raise KeyError(key)
    return value


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


@tool(
    effect="read",
    label="Get stored result",
    summary="Read result {result_id}",
    keywords=(
        "result_id",
        "expand result",
        "omitted items",
        "next page",
        "full result",
        "snapshot",
        "truncated",
    ),  # fmt: skip
    cost="light",
    runs="sidecar",
    example={"result_id": "result_0123456789abcdef0123456789abcdef", "field": "/data/curves"},
)
def nx_get_result(
    tx: Tx,
    *,
    result_id: ResultId,
    field: Pointer = "",
    offset: Offset = 0,
    limit: Limit = 20,
    detail: Detail = "compact",
) -> ResultData:
    """Page or expand a stored result by result_id without repeating the call or reaching NX.
    field is a JSON pointer from omitted; lists, object keys and text are paged (text: limit
    x 1000 characters). References inside may be stale after model changes."""
    path = _file(tx.workspace.root, result_id)
    if path is None:
        fail("NX_RESULT_EXPIRED", f"No stored result {result_id}", field="/result_id",
             hint="Repeat the original call; snapshots expire after nx_delete_results or "
             "retention pruning")  # fmt: skip
    try:
        with LOCK:
            stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail("NX_RESULT_EXPIRED", f"Stored result {result_id} is unreadable", field="/result_id")
    try:
        value = _select(stored, field)
    except KeyError:
        fail("NX_INVALID_ARGUMENT", f"field {field} is not in the result", field="/field",
             hint="Use a pointer from omitted, or read the parent to see its keys")  # fmt: skip
    omitted: dict = {}
    out: dict = {"result_id": result_id, "field": field}
    if isinstance(value, str):
        selected, page = result.page(value, offset, limit * 1000, maximum=100_000)
        out.update(kind="text", value="".join(selected), page=page)
    elif isinstance(value, (list, dict)):
        keys = list(value) if isinstance(value, dict) else range(len(value))
        chosen, page = result.page(keys, offset, limit, maximum=100)
        pointers = {k: f"{field}/{_escape(str(k))}" for k in chosen}
        rows = {
            k: value[k] if detail == "full" else compact(value[k], pointers[k], omitted)
            for k in chosen
        }
        if _size(rows) > MAX_RESPONSE:  # Too big together: name each entry instead.
            rows = {k: {"field": pointers[k], "read_separately": True} for k in chosen}
            omitted = {field or "/": {"reason": "Entries too large together; read each field"}}
        if isinstance(value, dict):
            out.update(kind="object", value=rows, page=page)
        else:
            out.update(kind="list", items=list(rows.values()), page=page)
    else:
        out.update(kind="value", value=value)
    if omitted:
        out["omitted"] = omitted
    return out  # type: ignore[return-value]  # built field by field


@tool(
    effect="file",
    label="Delete stored results",
    summary=lambda a, say: (
        "Preview stored result cleanup" if a.get("dry_run", True) else "Delete stored results"
    ),
    keywords=(
        "result cleanup",
        "prune snapshots",
        "delete result_id",
        "free disk space",
        "retention",
    ),  # fmt: skip
    cost="light",
    runs="sidecar",
    example={"dry_run": True, "max_age_seconds": 86400},
)
def nx_delete_results(
    tx: Tx,
    *,
    dry_run: Annotated[bool, Doc("true previews counts and bytes; false deletes")] = True,
    max_age_seconds: Annotated[int, Range(ge=1), Doc("Older snapshots are selected")] | None = None,
    max_bytes: Annotated[int, Range(ge=1), Doc("Oldest snapshots go until the total fits")]
    | None = None,
    result_ids: Annotated[list[ResultId], Items(1, 100), Doc("Select exactly these snapshots")]
    | None = None,
) -> CleanupData:
    """Preview (default) or delete stored result snapshots by age, total size or result_id.
    Defaults: 7 days, 256 MiB. Receipts and CAD files are never touched; deleted ids expire."""
    with tx.checks():
        try:
            policy = settings()
        except ValueError as error:
            fail("NX_INVALID_ARGUMENT", f"Retention settings: {error}")
    root = tx.workspace.root
    if result_ids:
        wanted = list(dict.fromkeys(result_ids))
        found = [(i, _file(root, i)) for i in wanted]
        present = [p for _, p in found if p is not None]
        sizes = {p: p.stat().st_size for p in present}
        total = sum(
            p.stat().st_size
            for folder in directories(root)
            if folder.is_dir()
            for p in folder.glob("result_*.json")
        )
        count = sum(
            1
            for folder in directories(root)
            if folder.is_dir()
            for _ in folder.glob("result_*.json")
        )
        if not dry_run and present:
            tx.touched = True
            with LOCK:
                for p in present:
                    p.unlink(missing_ok=True)
        return {
            "applied": not dry_run,
            "policy": policy,
            "snapshot_count": count,
            "bytes_before": total,
            "selected_count": len(present),
            "bytes_after_cleanup": total - sum(sizes.values()),
            "missing": [i for i, p in found if p is None],
        }
    out = {
        "applied": not dry_run,
        "policy": policy,
        "snapshot_count": 0,
        "bytes_before": 0,
        "selected_count": 0,
        "bytes_after_cleanup": 0,
        "missing": [],
    }
    for folder in directories(root):
        report = maintain(folder, apply=False, max_age_seconds=max_age_seconds, max_bytes=max_bytes)
        if not dry_run and report["selected_count"]:
            tx.touched = True
            report = maintain(
                folder, apply=True, max_age_seconds=max_age_seconds, max_bytes=max_bytes
            )
        out["policy"] = report["policy"]
        for key in ("snapshot_count", "bytes_before", "selected_count", "bytes_after_cleanup"):
            out[key] += report[key]
    return out  # type: ignore[return-value]  # built field by field

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

Several agents share the workspace's snapshots, each through its own sidecar process.
By age or size ``nx_delete_results`` prunes only the caller's own snapshots: those its
sidecar stored or received (``kit.snapshots.claimed``; after a sidecar restart the
earlier ones count as others'); ``all_agents`` prunes every agent's. ``result_ids`` names
exact snapshots (whoever stored them). The automatic retention (7 days, 256 MiB) still
prunes every snapshot.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Offset, Range, Text
from nx_mcp.kit import result
from nx_mcp.kit.snapshots import LOCK, bury, claimed, maintain, settings, tombstone

# The two snapshot folders under <workspace>/.nx-mcp: sidecar compaction, bridge overflow.
STORES = ("agent-results", "bridge-results")
COMPACT_ITEMS = 20
COMPACT_CHARS = 2048
MAX_RESPONSE = 400 * 1024
ID_PATTERN = r"^result_[0-9a-f]{32}$"

ResultId = Annotated[
    str, Text(39, ID_PATTERN), Doc("result_id of a result that cut arrays or text (see omitted)")
]
# A pointer without its leading "/" ("data/curves") is read as "/data/curves".
Pointer = Annotated[str, Text(512), Doc("JSON pointer from omitted, e.g. /data/items/3")]
Limit = Annotated[int, Range(ge=1, le=100)]
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
    applied: bool  # false for a preview (dry_run)
    # own: this caller's snapshots (counts and sizes too); all: every agent's
    # (all_agents); ids: exactly result_ids
    scope: Literal["own", "all", "ids"]
    deleted_count: int  # snapshots actually deleted (0 for a preview)
    policy: dict  # {max_age_seconds, max_bytes}
    # counts and sizes of the snapshots in scope (own, all, or the result_ids found)
    snapshot_count: int
    bytes_before: int
    selected_count: int
    bytes_after_cleanup: int
    workspace_snapshot_count: int  # every agent's snapshots in the workspace, any scope
    missing: list[str]  # requested result_ids that never existed in this workspace
    already_deleted: list[str]  # requested result_ids deleted or pruned earlier


# Units of max_age_seconds given as text ('7d', '12 hours'), in seconds.
DURATIONS = {"": 1, "s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60,
             "h": 3600, "hr": 3600, "hour": 3600, "d": 86400, "day": 86400,
             "w": 604800, "wk": 604800, "week": 604800}  # fmt: skip


def _amount(value, units: dict, field: str, example: str) -> int | None:
    """An int, or text such as '7d' or '12 hours', as a whole number of base units."""
    if value is None or isinstance(value, int):
        return value
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*", value)
    unit = match.group(2).lower() if match else ""
    if match and unit not in units and len(unit) > 2 and unit.endswith("s"):
        unit = unit[:-1]  # hours, days, weeks, mins, secs
    if not match or unit not in units:
        accepted = ", ".join(u for u in units if u)
        fail("NX_INVALID_ARGUMENT", f"{field[1:]} {value!r} is not understood",
             field=field, hint=f"Pass an integer or a number with one of {accepted}, e.g. "
             f"{example}")  # fmt: skip
    amount = int(float(match.group(1)) * units[unit])
    if amount < 1:
        fail("NX_INVALID_ARGUMENT", f"{field[1:]} must be at least 1", field=field)
    return amount


def directories(root) -> list[Path]:
    return [Path(root) / ".nx-mcp" / name for name in STORES]


def _file(root, result_id: str) -> Path | None:
    for folder in directories(root):
        path = folder / (result_id + ".json")
        if path.is_file() and not path.is_symlink():
            return path
    return None


def _missing(root, result_id: str) -> NoReturn:
    """Fail for a result_id with no snapshot: expired (a tombstone says when it was
    deleted or pruned) or unknown to this workspace."""
    fresh = (
        "Repeat the original call for a fresh result_id. Only compact results that "
        f"cut an array over {COMPACT_ITEMS} items or text over {COMPACT_CHARS} characters "
        "(listed in omitted), or results too large for the bridge, carry one; paged lists "
        "use page.next_offset instead"
    )
    for folder in directories(root):
        gone = tombstone(folder, result_id)
        if gone is not None:
            how = "pruned by retention" if gone.get("reason") == "pruned" else "deleted"
            fail("NX_RESULT_EXPIRED",
                 f"Stored result {result_id} expired: it was {how} at {gone.get('deleted_at')}",
                 field="/result_id", hint=fresh,
                 details={"state": gone.get("reason"), "deleted_at": gone.get("deleted_at")})  # fmt: skip
    fail("NX_NOT_FOUND",
         f"No stored result {result_id} exists in this workspace and none was deleted: it "
         "never existed here (check the id; results from another workspace are not shared)",
         field="/result_id", hint=fresh, details={"state": "unknown"})  # fmt: skip


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
    field: a JSON pointer from omitted; lists and object keys page by item, text by
    character (offset in characters, limit x 1000). References inside may be stale after
    model changes."""
    if field and not field.startswith("/"):
        field = "/" + field
    path = _file(tx.workspace.root, result_id)
    if path is None:
        _missing(tx.workspace.root, result_id)
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
    example={"dry_run": True, "max_age_seconds": "1d"},
)
def nx_delete_results(
    tx: Tx,
    *,
    dry_run: Annotated[bool, Doc("true previews counts and bytes; false deletes")] = True,
    max_age_seconds: Annotated[
        Annotated[int, Range(ge=1)] | Annotated[str, Text(16)],
        Doc("Older snapshots go: seconds, or '12h', '7d'"),
    ]
    | None = None,
    max_bytes: Annotated[int, Range(ge=1), Doc("Oldest snapshots go until the total fits")]
    | None = None,
    result_ids: Annotated[list[ResultId], Items(1, 100), Doc("Select exactly these snapshots")]
    | None = None,
    all_agents: Annotated[bool, Doc("Age/size: every agent's snapshots, not only yours")] = False,
) -> CleanupData:
    """Preview (default) or delete stored result snapshots by age, total size, or exactly
    result_ids (alone). Defaults: 7 days, 256 MiB, only your own snapshots. Receipts and CAD
    files are never touched."""
    with tx.checks():
        if result_ids:
            _ids_alone(max_age_seconds, max_bytes, all_agents)
        max_age_seconds = _amount(max_age_seconds, DURATIONS, "/max_age_seconds", "'7d'")
        try:
            policy = settings()
        except ValueError as error:
            fail("NX_INVALID_ARGUMENT", f"Retention settings: {error}")
    out = _delete(tx, dry_run, max_age_seconds, max_bytes, result_ids, policy, all_agents)
    out["deleted_count"] = out["selected_count"] if out["applied"] else 0
    if not out["deleted_count"]:
        tx.outcome = "not_applicable"  # a preview, or nothing matched: no file changed
    return out


def _ids_alone(max_age_seconds, max_bytes, all_agents) -> None:
    """result_ids select exactly those snapshots: an age, size or all_agents next to them
    would be ignored, so the call is refused instead."""
    given = [
        name
        for name, value in (
            ("max_age_seconds", max_age_seconds),
            ("max_bytes", max_bytes),
            ("all_agents", all_agents or None),
        )
        if value is not None
    ]
    if given:
        fail("NX_INVALID_ARGUMENT",
             f"result_ids selects exactly those snapshots; {', '.join(given)} would be ignored",
             field="/" + given[0],
             hint="Pass result_ids alone, or drop result_ids to prune by age or size")  # fmt: skip


def _workspace_count(root) -> int:
    return sum(
        1 for folder in directories(root) if folder.is_dir() for _ in folder.glob("result_*.json")
    )


def _gone_before(root, result_id: str) -> bool:
    """Whether a tombstone says the snapshot was deleted or pruned earlier."""
    return any(tombstone(folder, result_id) is not None for folder in directories(root))


def _delete(
    tx: Tx, dry_run, max_age_seconds, max_bytes, result_ids, policy, all_agents
) -> CleanupData:
    root = tx.workspace.root
    if result_ids:
        wanted = list(dict.fromkeys(result_ids))
        found = [(i, _file(root, i)) for i in wanted]
        present = [p for _, p in found if p is not None]
        sizes = {p: p.stat().st_size for p in present}
        absent = [i for i, p in found if p is None]
        count = _workspace_count(root)
        if not dry_run and present:
            tx.touched = True
            with LOCK:
                for p in present:
                    p.unlink(missing_ok=True)
                    bury(p.parent, [p.stem], "deleted")
        return {
            "applied": not dry_run,
            "scope": "ids",
            "deleted_count": 0,
            "policy": policy,
            "snapshot_count": len(present),
            "bytes_before": sum(sizes.values()),
            "selected_count": len(present),
            "bytes_after_cleanup": 0,
            "workspace_snapshot_count": count,
            "missing": [i for i in absent if not _gone_before(root, i)],
            "already_deleted": [i for i in absent if _gone_before(root, i)],
        }
    only = None if all_agents else claimed()
    out = {
        "applied": not dry_run,
        "scope": "all" if only is None else "own",
        "deleted_count": 0,
        "policy": policy,
        "snapshot_count": 0,
        "bytes_before": 0,
        "selected_count": 0,
        "bytes_after_cleanup": 0,
        "workspace_snapshot_count": 0,
        "missing": [],
        "already_deleted": [],
    }
    for folder in directories(root):
        limits = {"max_age_seconds": max_age_seconds, "max_bytes": max_bytes, "only": only}
        report = maintain(folder, apply=False, **limits)
        if not dry_run and report["selected_count"]:
            tx.touched = True
            report = maintain(folder, apply=True, **limits)
        out["policy"] = report["policy"]
        for key in ("snapshot_count", "bytes_before", "selected_count", "bytes_after_cleanup"):
            out[key] += report[key]
    out["workspace_snapshot_count"] = _workspace_count(root)
    return out  # type: ignore[return-value]  # built field by field

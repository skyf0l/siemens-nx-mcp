"""Stored result snapshots: retention of disposable response snapshots, never receipts.

Used by the sidecar result store, the bridge overflow store (``result_transport``)
and ``nx_get_result``/``nx_delete_results``. Every deleted or pruned snapshot leaves a
tombstone (``deleted.json`` in its folder: id -> when and why), so ``nx_get_result`` can
tell an expired result from one that never existed.

Several agents share a workspace, each through its own sidecar process, and several agents
may share one sidecar (one MCP connection): the snapshots a process stored or received
(:func:`claim`) are tagged with the caller that made the request (the sidecar's caller id,
:data:`OWNER`), and :func:`claimed` gives the current caller's own, so
``nx_delete_results`` prunes only those unless told otherwise (``maintain(only=...)``).
Callers the sidecar cannot tell apart (one id) share their snapshots; a snapshot claimed
without any caller id counts as every caller's.
"""

import contextvars
import json
import os
import re
import sys
import time
from pathlib import Path
from threading import RLock

LOCK = RLock()
# Result id -> the caller id that stored or received it (None: no caller id known).
_CLAIMED: dict = {}
# The caller of the request being served; unset, the sidecar's own caller variable is read.
OWNER: contextvars.ContextVar = contextvars.ContextVar("nx_snapshot_owner", default=None)
CURRENT = object()  # :func:`claimed`'s default: the current caller
PATTERN = re.compile(r"result_[a-f0-9]{32}\.json")
TOMBSTONES = "deleted.json"
MAX_TOMBSTONES = 5000  # the oldest are forgotten first


def settings():
    # NX_MCP_RESULT_MAX_AGE_SECONDS and NX_MCP_RESULT_MAX_BYTES (built here so that
    # kit string constants stay error codes only).
    defaults = {"max_age_seconds": "604800", "max_bytes": "268435456"}
    values = {k: int(os.environ.get(f"nx_mcp_result_{k}".upper(), v)) for k, v in defaults.items()}
    if any(v <= 0 for v in values.values()):
        raise ValueError("Snapshot retention age and byte limits must be positive integers")
    return values


def current_owner() -> str | None:
    """The caller id of the request being served (:data:`OWNER`, else the sidecar's
    ``caller`` of the request), None outside a request or without one."""
    owner = OWNER.get()
    if owner is None:
        proxy = sys.modules.get("nx_mcp.sidecar.proxy")
        caller = getattr(proxy, "CALLER", None)
        if caller is not None:
            try:
                owner = caller.get()
            except LookupError:
                owner = None
    return str(owner) if owner else None


def claim(result_id, owner=CURRENT) -> None:
    """Record ``result_id`` as a snapshot of ``owner`` (default: the current caller)."""
    who = current_owner() if owner is CURRENT else owner
    with LOCK:
        _CLAIMED[str(result_id)] = who


def claimed(owner=CURRENT) -> set:
    """The result ids this process stored or received for ``owner`` (default: the current
    caller; None: every caller's), snapshots without a caller id included (a copy)."""
    who = current_owner() if owner is CURRENT else owner
    with LOCK:
        if who is None:
            return set(_CLAIMED)
        return {k for k, v in _CLAIMED.items() if v is None or v == who}


def maintain(root, *, apply=False, max_age_seconds=None, max_bytes=None, protect=None,
             only=None):  # fmt: skip
    """Select (and with ``apply`` delete) the snapshots of the folder ``root`` older than
    the age limit, oldest first until the rest fit the byte limit. ``only`` (result ids):
    consider only those snapshots (counts and sizes too); None: all of them."""
    root = Path(root)
    config = settings()
    for key, value in {"max_age_seconds": max_age_seconds, "max_bytes": max_bytes}.items():
        if value is not None:
            if type(value) is not int or value <= 0:
                raise ValueError("Retention limits must be positive integers")
            config[key] = value
    with LOCK:
        records = []
        if root.exists():
            for p in root.iterdir():
                if only is not None and p.stem not in only:
                    continue
                if PATTERN.fullmatch(p.name) and p.is_file() and not p.is_symlink():
                    stat = p.stat()
                    records.append((stat.st_mtime, stat.st_size, p))
        records.sort(key=lambda x: (x[0], x[2].name))
        total = sum(size for _, size, _ in records)
        selected = []
        now = time.time()
        for modified, size, path in records:
            if path.stem == protect:
                continue
            if now - modified > config["max_age_seconds"] or total > config["max_bytes"]:
                selected.append(path)
                total -= size
        if apply:
            for path in selected:
                path.unlink(missing_ok=True)
            bury(root, [path.stem for path in selected], "pruned")
        return {
            "policy": config,
            "snapshot_count": len(records),
            "bytes_before": sum(x[1] for x in records),
            "selected_count": len(selected),
            "bytes_after_cleanup": total,
            "applied": apply,
            "recovery_receipts_affected": False,
        }


def _tombstones(folder: Path) -> dict:
    try:
        data = json.loads((folder / TOMBSTONES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def bury(root, result_ids, reason: str) -> None:
    """Record that ``result_ids`` were removed from the store folder ``root``
    (``reason``: ``deleted`` by nx_delete_results or ``pruned`` by retention).
    Never raises: a lost tombstone only makes the error message less specific."""
    ids = [str(i) for i in result_ids]
    if not ids:
        return
    folder = Path(root)
    with LOCK:
        try:
            index = _tombstones(folder)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for result_id in ids:
                index.pop(result_id, None)  # re-inserted last: newest at the end
                index[result_id] = {"deleted_at": stamp, "reason": reason}
            while len(index) > MAX_TOMBSTONES:
                index.pop(next(iter(index)))
            folder.mkdir(parents=True, exist_ok=True)
            temp = folder / (TOMBSTONES + ".tmp")
            temp.write_text(json.dumps(index), encoding="utf-8")
            os.replace(temp, folder / TOMBSTONES)
        except OSError:
            pass


def tombstone(root, result_id: str) -> dict | None:
    """``{"deleted_at", "reason"}`` of a removed snapshot of the folder ``root``, else None."""
    with LOCK:
        entry = _tombstones(Path(root)).get(str(result_id))
    return entry if isinstance(entry, dict) else None

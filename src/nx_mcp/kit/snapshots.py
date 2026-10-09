"""Stored result snapshots: retention of disposable response snapshots, never receipts.

Used by the sidecar result store, the bridge overflow store (``result_transport``)
and ``nx_get_result``/``nx_delete_results``.
"""

import os
import re
import time
from pathlib import Path
from threading import RLock

LOCK = RLock()
PATTERN = re.compile(r"result_[a-f0-9]{32}\.json")


def settings():
    # NX_MCP_RESULT_MAX_AGE_SECONDS and NX_MCP_RESULT_MAX_BYTES (built here so that
    # kit string constants stay error codes only).
    defaults = {"max_age_seconds": "604800", "max_bytes": "268435456"}
    values = {k: int(os.environ.get(f"nx_mcp_result_{k}".upper(), v)) for k, v in defaults.items()}
    if any(v <= 0 for v in values.values()):
        raise ValueError("Snapshot retention age and byte limits must be positive integers")
    return values


def maintain(root, *, apply=False, max_age_seconds=None, max_bytes=None, protect=None):
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
        return {
            "policy": config,
            "snapshot_count": len(records),
            "bytes_before": sum(x[1] for x in records),
            "selected_count": len(selected),
            "bytes_after_cleanup": total,
            "applied": apply,
            "recovery_receipts_affected": False,
        }

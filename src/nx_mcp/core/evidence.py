"""Evidence gating (gate 3, tool-surface decision 11).

``src/nx_mcp/evidence/<domain>.json`` maps tool -> build key (``v2406``) -> the
variants that passed machine-checked acceptance scenarios. Only the acceptance
runner writes these files (``record``). A build key present means tested.

Listings show only what can run: a tool's untested variants leave its schema enum
and its ``tool#variant`` schemas, and a tool with no variant left is hidden
(``variants_for``). Session license memory: a tested tool or variant that failed
with ``NX_LICENSE_UNAVAILABLE`` is refused before NX for the rest of the NX
session (``remember_license`` / ``license_refusal``); ``nx_get_status`` reports
the list so the sidecar hides it too. The memory lives in the NX process and is
kept across development host reloads, so only an NX restart clears it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import types
from pathlib import Path

from nx_mcp.core.errors import NXError

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "evidence"
# The supported build listings assume while no NX is connected (no bridge descriptor).
DEFAULT_BUILD = "v2406"
_CACHE: dict[str, dict] | None = None
_MEMORY_MODULE = "_nx_mcp_session_memory"  # not under nx_mcp: survives a host reload


def build_key(nx_version: str | None) -> str | None:
    """``v2406`` for ``"v2406"``, ``"NX 2406"``, ``"2406.1700"``...; None when unknown."""
    match = re.search(r"(?<!\d)(\d{4})(?!\d)", str(nx_version or ""))
    return "v" + match.group(1) if match else None


def load(directory: str | Path | None = None) -> dict[str, dict]:
    """``{tool: {build: entry}}`` merged over every domain file (cached)."""
    global _CACHE
    if directory is None and _CACHE is not None:
        return _CACHE
    merged: dict[str, dict] = {}
    root = Path(directory) if directory else EVIDENCE_DIR
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for tool, builds in (data.get("tools", data) or {}).items():
                if isinstance(builds, dict):
                    merged.setdefault(tool, {}).update(builds)
    if directory is None:
        _CACHE = merged
    return merged


def reload() -> None:
    global _CACHE
    _CACHE = None


def for_tool(name: str) -> dict:
    return dict(load().get(name, {}))


def known_build(build: str | None) -> bool:
    return bool(build) and any(build in builds for builds in load().values())


def available(spec, build: str | None, variant: str | None = None) -> bool:
    builds = load().get(spec.name, {})
    if not build or build not in builds:
        return False
    entry = builds[build] or {}
    if variant is None or not spec.variants:
        return True
    return variant in (entry.get("variants") or [])


def variants_for(spec, build: str | None) -> tuple[str, ...]:
    """The variant values usable on ``build``: tested ones (all of a read/view tool on an
    unknown build). A tool whose variants are sub-schemas (no variant field) is gated
    as a whole, so all its values count once the tool is tested."""
    values = spec.variant_values()
    if not values:
        return ()
    if not known_build(build):
        return values if spec.effect in ("read", "view") else ()
    if not available(spec, build):
        return ()
    if not isinstance(spec.variants, str):
        return values
    return tuple(v for v in values if available(spec, build, v))


def visible(spec, build: str | None) -> bool:
    """Discovery filter: tested on this build (at least one variant), or a read/view
    tool on an unknown build."""
    if known_build(build):
        return available(spec, build) and (
            not spec.variant_values() or bool(variants_for(spec, build))
        )
    return spec.effect in ("read", "view")


def check(spec, build: str | None, args: dict, *, dev: bool = False) -> NXError | None:
    """The gate-3 refusal for this call, or None.

    In development mode (``NX_MCP_DEV=1``) untested tools run, so the acceptance
    runner can produce their first evidence; production refuses them.
    """
    if dev:
        return None
    if not known_build(build):
        if spec.effect in ("read", "view"):
            return None
        reason = "this NX build has no acceptance evidence"
    else:
        variant = spec.variant_of(args)
        if available(spec, build, variant):
            return None
        reason = f"variant {variant!r} is not tested" if variant else "the tool is not tested"
    details = {"tool": spec.name, "build": build, "variant": spec.variant_of(args)}
    offered = variants_for(spec, build) if details["variant"] else ()
    if offered:  # only the variants this build offers
        reason += f"; available here: {', '.join(repr(v) for v in offered)}"
        details["available"] = list(offered)
    return NXError(
        "NX_VERSION_UNSUPPORTED",
        f"{spec.name} is not available on NX build {build or 'unknown'}: {reason}",
        details=details,
    )


# ---------------------------------------------------------------------------
# Session license memory (NX process)


def _memory() -> dict:
    holder = sys.modules.get(_MEMORY_MODULE)
    if holder is None:
        holder = types.ModuleType(_MEMORY_MODULE)
        holder.licenses = {}  # type: ignore[attr-defined]  # a bare module keeps the memory
        sys.modules[_MEMORY_MODULE] = holder
    return holder.licenses


LICENSE_MEMORY_SECONDS = 900.0  # a remembered license refusal is retried after this


def remember_license(spec, build: str | None, args, error) -> dict | None:
    """Remember a license failure of a tested tool/variant for this NX session."""
    if getattr(error, "code", None) != "NX_LICENSE_UNAVAILABLE":
        return None
    details = getattr(error, "details", None) or {}
    if details.get("remembered_from") or details.get("child_tool"):
        return None  # a remembered refusal, or a batch child (recorded on its own spec)
    if details.get("argument_license"):
        return None  # gated by this call's arguments (e.g. one referenced object), not the call
    variant = spec.variant_of(args if isinstance(args, dict) else {})
    if not available(spec, build_key(build), variant):
        return None  # untested: evidence already hides it
    entry = {
        "tool": spec.name,
        "variant": variant,
        "nx_code": getattr(error, "nx_code", None),
        "since": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "at": time.time(),
        "message": str(getattr(error, "message", error))[:300],
    }
    _memory().setdefault((spec.name, variant), entry)
    return entry


def licenses() -> list[dict]:
    """The remembered license failures of this NX session (for ``nx_get_status``), the
    last :data:`LICENSE_MEMORY_SECONDS` only: a floating or token license NX refused
    because the pool was used up at that moment may be free again, so NX is asked again."""
    memory = _memory()
    now = time.time()
    for key in [k for k, e in memory.items() if now - e.get("at", 0.0) > LICENSE_MEMORY_SECONDS]:
        del memory[key]
    return [dict(e) for _, e in sorted(memory.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))]


def forget_licenses() -> None:
    _memory().clear()


def remembered(entries, tool: str | None, variant: str | None) -> dict | None:
    """The entry of ``entries`` (status rows) that blocks ``tool``/``variant``, or None."""
    for entry in entries or ():
        if entry.get("tool") == tool and entry.get("variant") in (None, variant):
            return entry
    return None


def license_error(entry: dict) -> NXError:
    name = entry["tool"] + (f"#{entry['variant']}" if entry.get("variant") else "")
    return NXError(
        "NX_LICENSE_UNAVAILABLE",
        f"{name}: no NX license on this seat (remembered from {entry.get('since')}); "
        "refused for up to 15 minutes, then NX is asked again",
        hint="Tell the engineer which license is missing; use an alternative",
        nx_code=entry.get("nx_code"),
        details={
            "tool": entry["tool"],
            "variant": entry.get("variant"),
            "remembered_from": entry.get("since"),
        },
    )


def license_refusal(spec, args) -> NXError | None:
    """Gate 3, NX side: the remembered license refusal of this call, or None."""
    entry = remembered(licenses(), spec.name, spec.variant_of(args))
    return license_error(entry) if entry else None


def record(
    domain: str,
    tool: str,
    build: str,
    *,
    variants: list[str],
    scenario: str,
    scenario_sha256: str,
    source_sha256: str,
    checks: int,
    recorded: str,
    directory: str | Path | None = None,
) -> Path:
    """Writer API for the acceptance runner's ``fold``: record one passing scenario.

    ``recorded`` is the evidence file's own ``recorded`` time, never "now", so a fold
    of unchanged evidence rewrites nothing."""
    root = Path(directory) if directory else EVIDENCE_DIR
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{domain}.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    entry = data.setdefault(tool, {}).setdefault(build, {})
    entry.update(
        variants=sorted(set(entry.get("variants", [])) | set(variants)),
        scenario=scenario,
        scenario_sha256=scenario_sha256,
        source_sha256=source_sha256,
        checks=int(checks),
        recorded=recorded,
    )
    temp = path.with_suffix(".tmp")
    # Bytes with LF: identical inputs give byte-identical files on every OS.
    temp.write_bytes((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    os.replace(temp, path)
    if directory is None:
        reload()
    return path

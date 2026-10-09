"""Evidence gating (gate 3, tool-surface decision 11).

``src/nx_mcp/evidence/<domain>.json`` maps tool -> build key (``v2406``) -> the
variants that passed machine-checked acceptance scenarios. Only the acceptance
runner writes these files (``record``). A build key present means tested.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from nx_mcp.core.errors import NXError

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "evidence"
_CACHE: dict[str, dict] | None = None


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


def visible(spec, build: str | None) -> bool:
    """Discovery filter: tested on this build, or a read/view tool on an unknown build."""
    if known_build(build):
        return available(spec, build)
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
    return NXError(
        "NX_VERSION_UNSUPPORTED",
        f"{spec.name} is not available on NX build {build or 'unknown'}: {reason}",
        details={"tool": spec.name, "build": build, "variant": spec.variant_of(args)},
    )


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
    """Writer API for the acceptance runner: record one passing scenario."""
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
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)
    if directory is None:
        reload()
    return path

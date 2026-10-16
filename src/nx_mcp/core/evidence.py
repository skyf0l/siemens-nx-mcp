"""Evidence gating (gate 3, tool-surface decision 11).

``src/nx_mcp/evidence/<domain>.json`` maps tool -> build key (``v2406``) -> the
variants that passed machine-checked acceptance scenarios, with the full NX build
they ran on (``nx_build``, e.g. ``2406.9160``). Only the acceptance runner writes
these files (``record``). Gating goes by the build key; a key present means tested.

What is offered (``offered``, ``variants_for``, ``visible``): a tool or variant with
evidence on the connected build; else one with evidence on any other build, marked
untested (an untested NX build, or a variant tested elsewhere only); never one
tested nowhere. ``NX_MCP_UNTESTED=read_only`` (default ``offer``) keeps only read and
view tools on an untested build; a failed undo self-test does the same
(``self_test_result``). An untested build's tool that fails because NX lacks an
NXOpen member is remembered for the NX session (``remember_version``) and then
refused (``NX_VERSION_UNSUPPORTED``). Session license memory: an offered tool or variant
(on an untested build too) that failed with ``NX_LICENSE_UNAVAILABLE`` is refused before NX for ``LICENSE_MEMORY_SECONDS``
(``remember_license`` / ``license_refusal``), then NX is asked again: a floating or
token license may be free by then. ``nx_get_status`` reports the list so the sidecar
hides it too. The memory lives in the NX process and is kept across development host
reloads.
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
    """Whether ``build`` (a key) is tested: some tool has evidence there."""
    return bool(build) and any(build in builds for builds in load().values())


def build_label(nx_build: str | None) -> str:
    """``NX 2406.9160`` for NX's build text (``"2406.9160"``, ``"v2406"``, ``"NX 2406"``)."""
    text = " ".join(str(nx_build or "").split())
    if not text or build_key(text) is None:
        return "NX (build unknown)"
    return text if text.casefold().startswith("nx") else f"NX {text.lstrip('vV')}"


READ_EFFECTS = ("read", "view")
UNTESTED_MODES = ("offer", "read_only")


def untested_mode() -> str:
    """``NX_MCP_UNTESTED``: ``offer`` (default) or ``read_only`` (an untested NX build
    runs only read and view tools)."""
    value = os.environ.get("NX_MCP_UNTESTED", "").strip().casefold().replace("-", "_")
    return value if value in UNTESTED_MODES else "offer"


def restricted(build: str | None, read_only: bool = False) -> bool:
    """Whether only read and view tools run on ``build`` (a key): an untested build in
    read-only mode (``NX_MCP_UNTESTED``, a failed self-test, or ``read_only``: what the
    connected NX reported to the sidecar)."""
    if known_build(build):
        return False
    return bool(read_only) or untested_mode() == "read_only" or self_test_failed()


def available(spec, build: str | None, variant: str | None = None) -> bool:
    """Tested on ``build`` (a key): the tool, and ``variant`` when it is given."""
    builds = load().get(spec.name, {})
    if not build or build not in builds:
        return False
    entry = builds[build] or {}
    if variant is None or not spec.variants:
        return True
    return variant in (entry.get("variants") or [])


def tested_on(spec, variant: str | None = None) -> list[str]:
    """The build keys with evidence for ``spec`` (and ``variant``), sorted."""
    return sorted(b for b in load().get(spec.name, {}) if available(spec, b, variant))


def offered(spec, build: str | None, variant: str | None = None, *, read_only=False) -> bool:
    """Tested on ``build``, or tested on another build and allowed here (module notes)."""
    if available(spec, build, variant):
        return True
    if not tested_on(spec, variant):
        return False
    return spec.effect in READ_EFFECTS or not restricted(build, read_only)


def variants_for(spec, build: str | None, *, read_only=False) -> tuple[str, ...]:
    """The variant values offered on ``build`` (:func:`offered`). A tool whose variants
    are sub-schemas (no variant field) is gated as a whole, so all its values count
    once the tool is offered."""
    values = spec.variant_values()
    if not values:
        return ()
    if not isinstance(spec.variants, str):
        return values if offered(spec, build, read_only=read_only) else ()
    return tuple(v for v in values if offered(spec, build, v, read_only=read_only))


def visible(spec, build: str | None, *, read_only=False) -> bool:
    """Discovery filter: offered on this build (at least one variant)."""
    return offered(spec, build, read_only=read_only) and (
        not spec.variant_values() or bool(variants_for(spec, build, read_only=read_only))
    )


def check(
    spec, build: str | None, args: dict, *, dev: bool = False, read_only=False
) -> NXError | None:
    """The gate-3 refusal for this call, or None.

    In development mode (``NX_MCP_DEV=1``) untested tools run, so the acceptance
    runner can produce their first evidence; production refuses them.
    """
    if dev:
        return None
    variant = spec.variant_of(args)
    if offered(spec, build, variant, read_only=read_only):
        return None
    if not offered(spec, build, read_only=read_only) and tested_on(spec):
        reason = "this NX build is not tested with NX MCP and runs only read and view tools"
    elif variant and tested_on(spec):
        reason = f"variant {variant!r} is not tested on any NX build"
    else:
        reason = "the tool is not tested on any NX build"
    details = {"tool": spec.name, "build": build, "variant": variant}
    others = variants_for(spec, build, read_only=read_only) if variant else ()
    if others:  # only the variants this build offers
        reason += f"; available here: {', '.join(repr(v) for v in others)}"
        details["available"] = list(others)
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
    """Remember a license failure of an offered tool/variant (tested on this build or,
    on an untested build, on any build) for this NX session."""
    if getattr(error, "code", None) != "NX_LICENSE_UNAVAILABLE":
        return None
    details = getattr(error, "details", None) or {}
    if details.get("remembered_from") or details.get("child_tool"):
        return None  # a remembered refusal, or a batch child (recorded on its own spec)
    if details.get("argument_license"):
        return None  # gated by this call's arguments (e.g. one referenced object), not the call
    variant = spec.variant_of(args if isinstance(args, dict) else {})
    if not offered(spec, build_key(build), variant):
        return None  # tested nowhere: evidence already hides it
    entry = {
        "tool": spec.name,
        "variant": variant,
        "nx_code": getattr(error, "nx_code", None),
        "license": details.get("license"),  # the feature NX named or the tool tried
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
    feature = entry.get("license")
    details = {
        "tool": entry["tool"],
        "variant": entry.get("variant"),
        "remembered_from": entry.get("since"),
    }
    if feature:
        details["license"] = feature
    return NXError(
        "NX_LICENSE_UNAVAILABLE",
        f"{name}: NX refused it for a missing license"
        + (f" ({feature})" if feature else "")
        + f" on this seat (remembered from {entry.get('since')}); "
        "refused for up to 15 minutes, then NX is asked again",
        hint="Tell the engineer which license is missing (details.license); use an alternative",
        nx_code=entry.get("nx_code"),
        details=details,
    )


def license_refusal(spec, args) -> NXError | None:
    """Gate 3, NX side: the remembered license refusal of this call, or None."""
    entry = remembered(licenses(), spec.name, spec.variant_of(args))
    return license_error(entry) if entry else None


# ---------------------------------------------------------------------------
# Session version memory and the undo self-test (NX process, kept across host reloads)


def _session(name: str, default):
    """Attribute ``name`` of the session memory module (made with ``default``)."""
    holder = sys.modules.get(_MEMORY_MODULE)
    if holder is None:
        _memory()  # creates the module
        holder = sys.modules[_MEMORY_MODULE]
    if not hasattr(holder, name):
        setattr(holder, name, default)
    return getattr(holder, name)


VERSION_HINT = "Do not retry on this build; find another tool with nx_find_tools; tell the engineer"


def version_unsupported(spec, nx_build, variant, member: str) -> NXError:
    """``NX_VERSION_UNSUPPORTED`` of a tool that met a missing NXOpen ``member`` on an
    untested build (the kernel rolled the call back)."""
    return NXError(
        "NX_VERSION_UNSUPPORTED",
        f"{spec.name} is not supported on {build_label(nx_build)} (an NXOpen call it needs "
        "is missing); nothing changed",
        hint=VERSION_HINT,
        details={"tool": spec.name, "variant": variant, "build": nx_build, "member": member},
    )


def remember_version(
    spec, nx_build, variant, member: str, message: str = "", exception_type: str | None = None
) -> dict | None:
    """Remember for this NX session that ``spec`` (``variant``) cannot run on this build;
    never for a build where it is tested."""
    if available(spec, build_key(nx_build), variant):
        return None
    entry = {
        "tool": spec.name,
        "variant": variant,
        "build": nx_build,
        "member": member,
        "exception_type": exception_type,
        "since": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "message": str(message)[:300],
    }
    _session("versions", {}).setdefault((spec.name, variant), entry)
    return entry


def version_failures() -> list[dict]:
    """The tools this NX session found unsupported (for ``nx_get_status``)."""
    memory = _session("versions", {})
    return [dict(e) for _, e in sorted(memory.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))]


def forget_versions() -> None:
    _session("versions", {}).clear()


def version_error(entry: dict) -> NXError:
    """The refusal of a tool remembered unsupported (status row ``entry``)."""
    name = entry["tool"] + (f"#{entry['variant']}" if entry.get("variant") else "")
    return NXError(
        "NX_VERSION_UNSUPPORTED",
        f"{name} failed on {build_label(entry.get('build'))} (an NXOpen call it needs is "
        f"missing, since {entry.get('since')}); not run",
        hint=VERSION_HINT,
        details={
            "tool": entry["tool"],
            "variant": entry.get("variant"),
            "build": entry.get("build"),
            "member": entry.get("member"),
            "remembered_from": entry.get("since"),
        },
    )


def version_refusal(spec, args) -> NXError | None:
    """Gate 3, NX side: the remembered version failure of this call, or None."""
    entry = remembered(version_failures(), spec.name, spec.variant_of(args))
    return version_error(entry) if entry else None


def self_test_result() -> dict | None:
    """The undo self-test of this NX session (``{state, reason, at, seconds}``), or None
    before it ran. ``state``: ``passed``, ``failed`` (only read and view tools run) or
    ``skipped`` (it could not be set up; nothing is restricted)."""
    result = _session("self_test", None)
    return dict(result) if isinstance(result, dict) else None


def set_self_test(result: dict | None) -> None:
    _session("self_test", None)
    holder = sys.modules[_MEMORY_MODULE]
    holder.self_test = dict(result) if isinstance(result, dict) else None  # type: ignore[attr-defined]


def self_test_failed() -> bool:
    result = self_test_result()
    return result is not None and result.get("state") == "failed"


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
    nx_build: str | None = None,
) -> Path:
    """Writer API for the acceptance runner's ``fold``: record one passing scenario.

    ``build`` is the key that gates (``v2406``), ``nx_build`` NX's full build text
    (``2406.9160``), kept next to it. ``recorded`` is the evidence file's own
    ``recorded`` time, never "now", so a fold of unchanged evidence rewrites nothing."""
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
    if nx_build and build_key(nx_build) == build and str(nx_build) != build:
        entry["nx_build"] = str(nx_build)
    temp = path.with_suffix(".tmp")
    # Bytes with LF: identical inputs give byte-identical files on every OS.
    temp.write_bytes((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    os.replace(temp, path)
    if directory is None:
        reload()
    return path

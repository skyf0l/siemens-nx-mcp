"""Capability manifest loader.

capability_manifest.json plus capability_fragments/<group>.json, one fragment
per feature group under parallel development ({tool: {status, evidence_type,
scope}}). Fragments are folded into capability_manifest.json before a release.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent


def load_manifest() -> dict:
    manifest = json.loads((HERE / "capability_manifest.json").read_text(encoding="utf-8"))
    for fragment in sorted((HERE / "capability_fragments").glob("*.json")):
        manifest["tools"].update(json.loads(fragment.read_text(encoding="utf-8")))
    return manifest


def unavailable_reason(entry: dict, nx_version: str) -> str | None:
    """Why a tool is disabled on this NX version (manifest unavailable_on), or None."""
    return (entry.get("unavailable_on") or {}).get(nx_version)


def scope_to_version(manifest: dict, nx_version: str) -> dict:
    """Keep "tested" only where there is native evidence for the running NX version.

    Evidence for a version is an evidence_type of real_NX_<version>... or the
    version listed in verified_versions; anything else becomes experimental.
    A tool listed for this version in unavailable_on is unavailable: the
    executor refuses it (NX_UNAVAILABLE_ON_VERSION) before touching NX.
    """
    for entry in manifest["tools"].values():
        if reason := unavailable_reason(entry, nx_version):
            entry.update(status="unavailable", scope=f"Not available on NX {nx_version}: {reason}")
            continue
        versions = set(entry.get("verified_versions", []))
        evidence = str(entry.get("evidence_type", ""))
        if evidence.startswith("real_NX_"):
            versions.add(evidence.split("_")[2])
        if entry.get("status") == "tested" and nx_version not in versions:
            entry.update(
                status="experimental",
                scope=f"Not tested on NX {nx_version}; evidence: {entry.get('scope', '')}",
            )
    return manifest

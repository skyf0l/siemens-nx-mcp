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

"""Render evidence-scoped capability documentation without importing NX or the server.

Two sources: the catalogue tools (``nx_mcp.core.catalogue``, stdlib only) with the
acceptance runner's evidence in ``src/nx_mcp/evidence/*.json``, and the legacy tools
that remain in the capability manifest until their domain migrates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("Native-tested", "Contract/sidecar-tested", "Experimental", "Unavailable")


def load_catalogue() -> list[dict]:
    """One row per catalogue tool with its evidence per NX build (no NX, no server)."""
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from nx_mcp.core import catalogue, evidence

    found = evidence.load(ROOT / "src/nx_mcp/evidence")
    return [
        {
            "name": name,
            "domain": spec.domain,
            "effect": spec.effect,
            "variants": list(spec.variant_values()),
            "evidence": found.get(name, {}),
        }
        for name, spec in sorted(catalogue.load().items())
    ]


def catalogue_classification(row: dict) -> str:
    """Native-tested only with acceptance evidence for a build; variants are listed."""
    return "Native-tested" if row.get("evidence") else "Experimental"


def catalogue_scope(row: dict) -> str:
    parts = []
    for build, entry in sorted((row.get("evidence") or {}).items()):
        tested = sorted(entry.get("variants") or [])
        missing = sorted(set(row.get("variants") or []) - set(tested))
        what = "all variants" if row.get("variants") and not missing else ", ".join(tested)
        text = f"{build}: {what or 'tested'}"
        if missing:
            text += f" (untested: {', '.join(missing)})"
        scenario = entry.get("scenario")
        if scenario:
            text += f"; {entry.get('checks', 0)} checks in {scenario}"
        parts.append(text)
    return "; ".join(parts) or "no acceptance evidence: hidden and refused in production"


def load_manifest() -> dict:
    """The full manifest (base plus capability_fragments) via nx_mcp.manifest, by path."""
    return runpy.run_path(str(ROOT / "src/nx_mcp/manifest.py"))["load_manifest"]()


def classification(entry: dict) -> str:
    """Fail closed: a tested status alone never establishes native evidence."""
    status = entry.get("status")
    evidence = entry.get("evidence_type", "")
    if status == "unavailable":
        return "Unavailable"
    if status != "tested":
        return "Experimental"
    if isinstance(evidence, str) and evidence.startswith("real_NX_"):
        return "Native-tested"
    if evidence in {
        "local_contract_test",
        "local_contract_tests",
        "live_sidecar_and_contract_tests",
    }:
        return "Contract/sidecar-tested"
    return "Experimental"


def cell(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
    )


def render(manifest: dict, catalogue: list[dict] | None = None) -> str:
    catalogue = list(catalogue or [])
    names = {row["name"] for row in catalogue}
    legacy = {k: v for k, v in manifest["tools"].items() if k not in names}
    canonical = json.dumps(
        {"manifest": manifest, "catalogue": catalogue}, sort_keys=True, separators=(",", ":")
    ).encode()
    tools = legacy
    counts = Counter(classification(entry) for entry in tools.values())
    counts.update(catalogue_classification(row) for row in catalogue)
    lines = [
        "# Capability evidence matrix",
        "",
        "Generated from the catalogue (`nx_mcp.core.catalogue`) with the acceptance evidence in "
        "`src/nx_mcp/evidence/`, and for the remaining legacy tools from "
        "`src/nx_mcp/capability_manifest.json` plus "
        "`src/nx_mcp/capability_fragments/` (`nx_mcp.manifest.load_manifest()`); "
        "do not edit this table by hand.",
        "Run `python scripts/generate_capability_matrix.py` to regenerate, or add `--check` to detect drift.",
        "",
        f"Manifest revision: **{cell(manifest['revision'])}**. NX: **{cell(manifest['nx_version'])}**. "
        f"Bridge protocol: **{cell(manifest['bridge_protocol'])}**.",
        f"Canonical SHA-256 (manifest and catalogue evidence): `{hashlib.sha256(canonical).hexdigest()}`.",
        "",
        "These labels report recorded evidence, not certification or independent verification of its claims. "
        "A catalogue tool is Native-tested when the acceptance runner recorded machine-checked "
        "scenario evidence for an NX build (listed per variant); without it the tool is "
        "Experimental, hidden from discovery and refused in production. "
        "A legacy tool is Native-tested with manifest status `tested` and an evidence type beginning `real_NX_`; "
        "only the stated scope and NX version are covered. Contract/sidecar-tested does not establish "
        "native CAD correctness. Experimental includes untested entries and tested entries without a "
        "recognized evidence type. Unavailable capabilities are explicitly recorded by the manifest; "
        "absence from this matrix is not proof of availability or unavailability.",
        "",
        "| Tool classification | Count |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {group} | {counts[group]} |" for group in GROUPS)
    lines.extend(
        [
            "",
            "## Catalogue tools",
            "",
            "| Tool | Classification | Domain | Effect | Acceptance evidence |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in catalogue:
        values = [
            row["name"],
            catalogue_classification(row),
            row["domain"],
            row["effect"],
            catalogue_scope(row),
        ]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    if not catalogue:
        lines.append("| none | | | | |")
    lines.extend(
        [
            "",
            "## Legacy tools",
            "",
            "| Tool | Classification | Manifest status | Evidence type | Tested scope / caveat |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for name, entry in sorted(tools.items()):
        values = [
            name,
            classification(entry),
            entry.get("status", "missing"),
            entry.get("evidence_type", "not recorded"),
            entry.get("scope", "not recorded"),
        ]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    lines.extend(
        [
            "",
            "## Explicitly unavailable capabilities",
            "",
            "These are capability names, separate from the tool counts above.",
            "",
        ]
    )
    lines.extend(f"- {cell(name)}" for name in sorted(manifest.get("unavailable", [])))
    if not manifest.get("unavailable"):
        lines.append("None recorded.")
    lines.extend(["", "## Manifest limitations", ""])
    lines.extend(f"- {cell(value)}" for value in manifest.get("limitations", []))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="render this single manifest file instead of the full load_manifest() view",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "docs/capability-matrix.md")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    manifest = (
        json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else load_manifest()
    )
    result = render(manifest, None if args.manifest else load_catalogue())
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != result:
            print(f"Capability matrix is stale: {args.output}; regenerate with this script.")
            return 1
        print("Capability matrix matches the catalogue evidence and the manifest.")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

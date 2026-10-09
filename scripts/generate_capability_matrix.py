"""Render evidence-scoped capability documentation without importing NX or the server.

The source is the catalogue (``nx_mcp.core.catalogue``, stdlib only) with the
acceptance runner's evidence in ``src/nx_mcp/evidence/*.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("Native-tested", "Experimental")


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


def render(catalogue: list[dict]) -> str:
    canonical = json.dumps({"catalogue": catalogue}, sort_keys=True, separators=(",", ":")).encode()
    counts = Counter(catalogue_classification(row) for row in catalogue)
    lines = [
        "# Capability evidence matrix",
        "",
        "Generated from the catalogue (`nx_mcp.core.catalogue`) with the acceptance evidence in "
        "`src/nx_mcp/evidence/`; do not edit this table by hand.",
        "Run `python scripts/generate_capability_matrix.py` to regenerate, or add `--check` to detect drift.",
        "",
        f"Canonical SHA-256 (catalogue evidence): `{hashlib.sha256(canonical).hexdigest()}`.",
        "",
        "These labels report recorded evidence, not certification or independent verification of its claims. "
        "A catalogue tool is Native-tested when the acceptance runner recorded machine-checked "
        "scenario evidence for an NX build (listed per variant); only that scope and build are "
        "covered. Without it the tool is Experimental, hidden from discovery and refused in production.",
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
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/capability-matrix.md")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = render(load_catalogue())
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != result:
            print(f"Capability matrix is stale: {args.output}; regenerate with this script.")
            return 1
        print("Capability matrix matches the catalogue evidence.")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

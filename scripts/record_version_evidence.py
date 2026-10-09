"""Fold live regression results into the capability manifest for one NX version.

Regression agents write docs/features/regression-<version>/<agent>.json:
{"nx_tool": {"verdict": "pass" | "disabled", "evidence": ..., "reason": ...}}.
A pass adds the version to the tool's verified_versions; a disabled tool gets
unavailable_on[<version>] = reason, so the executor refuses it on that version.

    python scripts/record_version_evidence.py v2406
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "src" / "nx_mcp" / "capability_manifest.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="NX version key, e.g. v2406")
    args = parser.parse_args()
    folder = ROOT / "docs" / "features" / f"regression-{args.version.removeprefix('v')}"
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    tools = manifest["tools"]
    problems, counts = [], {"pass": 0, "disabled": 0}
    for path in sorted(folder.glob("*.json")):
        for name, result in json.loads(path.read_text(encoding="utf-8")).items():
            entry = tools.get(name)
            verdict = result.get("verdict")
            if entry is None:
                problems.append(f"{path.name}: {name} is not in capability_manifest.json")
                continue
            versions = set(entry.get("verified_versions", []))
            unavailable = dict(entry.get("unavailable_on", {}))
            if verdict == "pass":
                versions.add(args.version)
                unavailable.pop(args.version, None)
            elif verdict == "disabled" and str(result.get("reason", "")).strip():
                versions.discard(args.version)
                unavailable[args.version] = result["reason"].strip()
            else:
                problems.append(f"{path.name}: {name} has no pass/disabled verdict with a reason")
                continue
            counts[verdict] += 1
            entry["verified_versions"] = sorted(versions)
            if unavailable:
                entry["unavailable_on"] = unavailable
            else:
                entry.pop("unavailable_on", None)
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{args.version}: {counts['pass']} pass, {counts['disabled']} disabled")
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

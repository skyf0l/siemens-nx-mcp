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

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "nx_mcp"
ROOT = PACKAGE.parents[1]
MANIFEST = PACKAGE / "capability_manifest.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="NX version key, e.g. v2406")
    args = parser.parse_args()
    folder = ROOT / "docs" / "features" / f"regression-{args.version.removeprefix('v')}"
    # A tool is defined in the manifest or in one capability fragment; update it there.
    documents = {MANIFEST: json.loads(MANIFEST.read_text(encoding="utf-8"))}
    tools = {name: (MANIFEST, e) for name, e in documents[MANIFEST]["tools"].items()}
    for fragment in sorted((PACKAGE / "capability_fragments").glob("*.json")):
        documents[fragment] = json.loads(fragment.read_text(encoding="utf-8"))
        tools.update({name: (fragment, e) for name, e in documents[fragment].items()})
    changed = set()
    problems, counts = [], {"pass": 0, "disabled": 0}
    for path in sorted(folder.glob("*.json")):
        for name, result in json.loads(path.read_text(encoding="utf-8")).items():
            source, entry = tools.get(name, (None, None))
            verdict = result.get("verdict")
            if entry is None:
                problems.append(f"{path.name}: {name} is not a manifest or fragment tool")
                continue
            changed.add(source)
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
    for source in changed:
        text = json.dumps(documents[source], indent=2, ensure_ascii=False) + "\n"
        source.write_text(text, encoding="utf-8", newline="\n")  # LF, as in the repository
    print(f"{args.version}: {counts['pass']} pass, {counts['disabled']} disabled")
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

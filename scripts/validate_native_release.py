"""Run release acceptance serially against a live graphical NX session.

Run only trusted source from a reviewed release. This never deploys or restarts NX.
Requires NX_MCP_URL (the HTTP surface, for the session snapshots); NX_MCP_WORKSPACE
(default <home>/NX_MCP_WORKSPACE) must name the workspace NX serves. The acceptance runner (``python -m nx_mcp.acceptance run``)
replays every scenario of acceptance/<domain>/ through a fresh stdio sidecar and
writes its evidence under --output, never into the installed source tree. Stops at
the first failed domain and retains all logs/evidence; no blind mutation retry, no
parallel NX calls.
"""

import argparse
import asyncio
import hashlib
import json
import os
import runpy
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# The public tool count: what the full profile lists in development mode, from this
# checkout's catalogue (scripts/public_tool_count.py, loaded by path): one source of truth.
EXPECTED_TOOL_COUNT = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "public_tool_count.py")
)["count"]()

# Domains in release order; each runs as one acceptance-runner process.
DOMAINS = [
    "session",
    "parts_files",
    "sketch",
    "modeling",
    "freeform",
    "sheet_metal",
    "assembly",
    "drawing",
    "inspection",
    "display",
]


def manifest_files(root):
    return [
        {
            "path": p.relative_to(root).as_posix(),
            "size": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "release-validation.json"
    ]


def verify_session(before, after):
    def parts(snapshot):
        return sorted(
            (p["path"], p["work"], p["display"], p["modified"]) for p in snapshot["parts"]
        )

    if parts(before) != parts(after):
        raise RuntimeError("Original open parts, work/display state or saved state changed")
    if before["components"] != after["components"]:
        raise RuntimeError("Original component source paths or transforms changed")


async def snapshot():
    async with (
        streamablehttp_client(os.environ["NX_MCP_URL"]) as (read, write, _),
        ClientSession(read, write) as client,
    ):
        await client.initialize()

        async def call(name):
            result = await client.call_tool(name, {})
            if result.isError:
                raise RuntimeError((name, result.structuredContent))
            return result.structuredContent

        status = await call("nx_get_status")
        if (status["data"]["control"] or {}).get("mode") != "agent":
            raise RuntimeError("NX must be in agent mode before native acceptance")
        parts = (await call("nx_list_parts"))["data"]["items"]
        if not parts or any(p["modified"] for p in parts):
            raise RuntimeError(
                "Keep an original saved part open and save existing work before acceptance"
            )
        components = await call("nx_list_components")

        # Strip session-scoped IDs, preserve all returned source paths/poses.
        def stable(value):
            if isinstance(value, dict):
                return {
                    k: stable(v)
                    for k, v in value.items()
                    if k
                    not in {
                        "id",
                        "part_id",
                        "session_id",
                        "generation_id",
                        "operation_id",
                        "warnings",
                        "mutation_outcome",
                        "status",
                    }
                }
            if isinstance(value, list):
                return [stable(v) for v in value]
            return value

        return {
            "parts": parts,
            "components": stable(components),
            "nx_version": status["data"]["bridge"]["nx_version"],
            "tool_count": len((await client.list_tools()).tools),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-tool-count", type=int, default=EXPECTED_TOOL_COUNT)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError(
            "Use a new output directory so receipts cannot be confused with an earlier run"
        )
    args.output.mkdir(parents=True)
    report = {"started": datetime.now(timezone.utc).isoformat(), "suites": [], "passed": False}
    source = Path(__file__).resolve().parents[1]
    metadata = source.parent / "release.json"
    if metadata.is_file():
        report["installed_release"] = json.loads(metadata.read_text())
    report["runtime_source"] = [
        f
        for f in manifest_files(source / "src" / "nx_mcp")
        if Path(f["path"]).suffix in {".py", ".json"}
    ]
    before = None
    try:
        before = asyncio.run(snapshot())
        report["before"] = before
        if before["tool_count"] != args.expected_tool_count:
            raise RuntimeError("Deployed tool count differs from the selected release")
        evidence = args.output / "evidence"
        for domain in DOMAINS:
            log = args.output / f"acceptance-{domain}.log"
            with log.open("w") as stream:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "nx_mcp.acceptance",
                        "run",
                        domain,
                        "--owner",
                        "release-acceptance",
                        "--evidence",
                        str(evidence.absolute()),
                    ],
                    cwd=source,
                    env={**os.environ, "NX_MCP_SOURCE_ROOT": str(source)},
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            records = sorted(evidence.glob(f"*/{domain}/*.json"))
            item = {
                "domain": domain,
                "exit_code": result.returncode,
                "evidence": [p.relative_to(args.output).as_posix() for p in records],
            }
            report["suites"].append(item)
            if result.returncode:
                raise RuntimeError(f"Native acceptance failed: {domain}; inspect {log}")
            failed = [p.name for p in records if p.name.endswith(".last-failure.json")]
            if not records or failed:
                raise RuntimeError(f"Missing or failed acceptance evidence: {domain} {failed}")
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        try:
            if before is not None:
                report["after"] = asyncio.run(snapshot())
                verify_session(before, report["after"])
                report["session_restored"] = True
        except Exception as error:
            report["passed"] = False
            report["restoration_error"] = str(error)
            raise
        finally:
            report["artifacts"] = manifest_files(args.output)
            report["finished"] = datetime.now(timezone.utc).isoformat()
            (args.output / "release-validation.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "suites": len(report["suites"]),
                "report": str(args.output / "release-validation.json"),
            }
        )
    )


if __name__ == "__main__":
    main()

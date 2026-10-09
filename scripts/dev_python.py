"""Run a Python snippet inside NX through the development launcher's bridge.

Development only (the startup app with NX_MCP_DEV=1); hold the NX lock
(scripts/dev_lock.py). Names available: NXOpen, session, part (work part),
services (NXServices), resolve(ref). print() output and `result` are returned. The
snippet runs under an undo mark: an exception rolls its model changes back.

    python scripts/dev_python.py "print(dir(NXOpen.Features.HoleFeatureBuilder))"
    python scripts/dev_python.py --file probe.py
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from nx_mcp.bridge import DescriptorBridgeClient


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("code", nargs="?")
    parser.add_argument("--file", type=Path)
    args = parser.parse_args()
    if bool(args.code) == bool(args.file):
        parser.error("pass code or --file")
    code = args.file.read_text(encoding="utf-8") if args.file else args.code
    descriptor = os.environ.get(
        "NX_MCP_BRIDGE_DESCRIPTOR",
        str(Path(os.environ["LOCALAPPDATA"]) / "nx-mcp" / "interactive-bridge.json"),
    )
    client = DescriptorBridgeClient(descriptor)
    try:
        result = asyncio.run(client.call("nx_dev_python", {"code": code}))
    except Exception as error:  # Show NX tracebacks verbatim.
        details = getattr(error, "details", {})
        print(f"ERROR {getattr(error, 'code', type(error).__name__)}: {error}")
        if details.get("traceback"):
            print(details["traceback"])
        return 1
    if result.get("status") == "error":
        print(json.dumps(result, indent=1)[:20000])
        return 1
    if result.get("stdout"):
        print(result["stdout"], end="" if result["stdout"].endswith("\n") else "\n")
    if result.get("result") not in (None, "None"):
        print("result =", result["result"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

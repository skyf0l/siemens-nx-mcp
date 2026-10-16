"""Run a Python snippet inside NX through the development launcher's bridge.

Development only (the startup app with NX_MCP_DEV=1). Only the holder of the
development lock probes: --owner <agent> must hold scripts/dev_lock.py's lock, else
the snippet is refused before NX is called (exit 3); --force-unlocked skips the check
with a warning (orchestrator only, when no agent uses NX). Names available: NXOpen, session, part (work part),
services (NXServices), resolve(ref). print() output and `result` are returned. The
snippet runs under an undo mark: an exception rolls its model changes back.

A probe works in its own part: create or open a disposable part first (under
<workspace>/smoke/<agent>/). With no work part in NX the snippet is refused before it
touches NX (NX_NO_WORK_PART): NXOpen calls on objects of a missing part open NX's
blocking "Null tag not allowed / Hit OK to continue" alert for everyone. --no-part runs
it anyway, for a snippet that creates or opens its part itself.

The snippet runs on NX's UI thread and stops itself after --timeout seconds (default
120, 0: none; checked between Python lines): the call fails NX_CANCELLED, rolled back,
naming the line it stopped at. One blocking NX call cannot be interrupted: the limit
acts when it returns, and NX stays busy until then.

    python scripts/dev_python.py --owner <agent> "print(dir(NXOpen.Features.HoleFeatureBuilder))"
    python scripts/dev_python.py --owner <agent> --file probe.py --timeout 300
    python scripts/dev_python.py --owner <agent> --no-part --file make_probe_part.py

Exit codes: 0 done, 1 the snippet or the call failed, 3 the lock is not held by --owner.
"""

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

from nx_mcp.bridge import (
    DEV_PYTHON_MARGIN,
    DEV_PYTHON_TIMEOUT,
    DEV_PYTHON_TIMEOUT_MAX,
    QUEUE_TIMEOUT,
    DescriptorBridgeClient,
)
from nx_mcp.workspace import default_workspace


def _seconds(text: str) -> float:
    value = float(text)
    if not 0 <= value <= DEV_PYTHON_TIMEOUT_MAX:
        raise argparse.ArgumentTypeError(f"0 to {DEV_PYTHON_TIMEOUT_MAX:g} seconds")
    return value


def _dev_reload():
    """scripts/dev_reload.py (its development-lock check), loaded by path."""
    path = Path(__file__).resolve().with_name("dev_reload.py")
    spec = importlib.util.spec_from_file_location("nx_mcp_dev_reload", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("code", nargs="?")
    parser.add_argument("--file", type=Path)
    parser.add_argument(
        "--timeout",
        type=_seconds,
        default=DEV_PYTHON_TIMEOUT,
        help=f"stop the snippet after this many seconds (default {DEV_PYTHON_TIMEOUT:g}, 0: none)",
    )
    parser.add_argument(
        "--no-part",
        action="store_true",
        help="run even when NX has no work part (the snippet creates or opens its own part)",
    )
    parser.add_argument("--owner", help="the holder of the development lock (dev_lock.py)")
    parser.add_argument(
        "--force-unlocked",
        action="store_true",
        help="skip the lock check (orchestrator only, when no agent uses NX)",
    )
    parser.add_argument("--workspace", default=str(default_workspace()))
    args = parser.parse_args(argv)
    if bool(args.code) == bool(args.file):
        parser.error("pass code or --file")
    reload_script = _dev_reload()
    refusal = reload_script.lock_refusal(args.workspace, args.owner, args.force_unlocked)
    if refusal is not None:
        print(refusal)
        return reload_script.LOCK_REFUSED
    code = args.file.read_text(encoding="utf-8") if args.file else args.code
    descriptor = os.environ.get(
        "NX_MCP_BRIDGE_DESCRIPTOR",
        str(Path(os.environ["LOCALAPPDATA"]) / "nx-mcp" / "interactive-bridge.json"),
    )
    params: dict = {"code": code, "timeout": args.timeout}
    if args.no_part:
        params["allow_no_part"] = True
    # Wait for NX's queue, the snippet's own limit and a margin: its timeout answer
    # arrives before this client gives up (0: no limit, the client's default wait).
    wait = QUEUE_TIMEOUT + args.timeout + 2 * DEV_PYTHON_MARGIN
    client = DescriptorBridgeClient(descriptor, timeout=wait)
    try:
        result = asyncio.run(client.call("nx_dev_python", params))
    except Exception as error:  # Show NX tracebacks verbatim.
        details = getattr(error, "details", None) or {}
        code_name = getattr(error, "code", type(error).__name__)
        print(f"ERROR {code_name}: {error}")
        if code_name == "NX_CANCELLED" and details.get("timeout_seconds") is not None:
            print(f"The snippet hit its {details['timeout_seconds']:g} s limit (--timeout).")
        if details.get("stdout"):
            print("--- output before the stop ---")
            print(details["stdout"], end="" if details["stdout"].endswith("\n") else "\n")
        if details.get("traceback"):
            print(details["traceback"])
        hint = getattr(error, "hint", None)
        if hint:
            print("hint:", hint)
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

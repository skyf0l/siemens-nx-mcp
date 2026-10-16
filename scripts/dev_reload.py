"""Reload the NX-side MCP host from disk via the NX startup application and wait.

Requires NX started with the startup app in development mode
(scripts/dev_start_nx.py --user-dir nx_app --env NX_MCP_DEV=1). Exit code 0 when
the new host started (and, with --wait-ready, published its panel state), 1 when
the code failed to load (the traceback is printed), 2 on timeout, 3 when --owner does
not hold the development lock (scripts/dev_lock.py; NX untouched).

A reload restarts the host for every agent, so only the lock holder reloads:

    python scripts/dev_lock.py acquire --owner <agent>
    python scripts/dev_reload.py --owner <agent> --wait-ready

--force-unlocked skips the check (with a warning): for the orchestrator when no agent
uses NX, never while another agent may hold the lock.
"""

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

LOCK_REFUSED = 3  # exit code: --owner does not hold the development lock


def _dev_lock():
    """scripts/dev_lock.py, loaded by path (this script also runs from other folders)."""
    path = Path(__file__).resolve().with_name("dev_lock.py")
    spec = importlib.util.spec_from_file_location("nx_mcp_dev_lock", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lock_refusal(workspace, owner, force_unlocked=False):
    """None when ``owner`` holds the development lock (refreshed by the check) or the
    check is skipped (``force_unlocked``, with a warning); else the refusal to print."""
    if force_unlocked:
        print(
            "WARNING: --force-unlocked: the development lock is not checked; another "
            "agent may be using NX.",
            file=sys.stderr,
        )
        return None
    if not owner:
        return "Pass --owner <agent> (the holder of scripts/dev_lock.py's lock). NX untouched."
    lock = _dev_lock()
    path = lock.lock_path(str(workspace))
    try:
        if lock.refresh(path, owner):
            return None
    except (OSError, TimeoutError) as error:
        return f"Cannot check the development lock ({error}). NX untouched."
    return (
        f"The development lock is not held by {owner}: {lock.describe(lock.read(path))}. "
        f"Take it first (python scripts/dev_lock.py acquire --owner {owner}). NX untouched."
    )


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # The default of nx_mcp.workspace.default_workspace(), without importing nx_mcp.
    parser.add_argument(
        "--workspace",
        default=os.environ.get("NX_MCP_WORKSPACE") or str(Path.home() / "NX_MCP_WORKSPACE"),
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--wait-ready",
        "--wait-agent",  # the old name
        dest="wait_ready",
        action="store_true",
        help="also wait until the new host has published its panel state",
    )
    parser.add_argument("--owner", help="the holder of the development lock (dev_lock.py)")
    parser.add_argument(
        "--force-unlocked",
        action="store_true",
        help="skip the lock check (orchestrator only, when no agent uses NX)",
    )
    args = parser.parse_args(argv)
    refusal = lock_refusal(args.workspace, args.owner, args.force_unlocked)
    if refusal is not None:
        print(refusal)
        return LOCK_REFUSED
    state_dir = Path(args.workspace) / ".nx-mcp"
    state_file = state_dir / "dev-state.json"
    before = read_json(state_file)
    if not before or not before.get("running"):
        print("Development journal is not running in NX (no running dev-state.json).")
        return 2
    (state_dir / "dev-restart").write_text("", encoding="utf-8")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        state = read_json(state_file)
        if state and state.get("generation", 0) > before.get("generation", 0):
            if not state.get("ok"):
                print(f"Generation {state['generation']} failed to load: {state.get('error')}")
                error = state_dir / "resident-error.txt"
                if error.exists():
                    print(error.read_text(encoding="utf-8"))
                return 1
            if not args.wait_ready:
                print(f"Generation {state['generation']} running.")
                return 0
            ui = read_json(state_dir / "ui-state.json") or {}
            if ui.get("sampled_at", 0) > state["updated_at"]:
                mode = ui.get("mode_label") or ui.get("mode")
                print(f"Generation {state['generation']} running; panel mode {mode}.")
                return 0
        time.sleep(0.2)
    print("Timed out waiting for the development journal (is NX busy or showing a dialog?).")
    return 2


if __name__ == "__main__":
    sys.exit(main())

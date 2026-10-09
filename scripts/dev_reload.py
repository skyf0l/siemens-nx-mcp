"""Reload the NX-side MCP host from disk via the NX startup application and wait.

Requires NX started with the startup app in development mode
(scripts/dev_start_nx.py --user-dir nx_app --env NX_MCP_DEV=1). Exit code 0 when
the new host started (and, with --wait-agent, the agent has control), 1 when
the code failed to load (the traceback is printed), 2 on timeout.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=os.environ.get("NX_MCP_WORKSPACE"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--wait-agent", action="store_true", help="also wait for agent control")
    args = parser.parse_args()
    if not args.workspace:
        parser.error("set NX_MCP_WORKSPACE or pass --workspace")
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
            if not args.wait_agent:
                print(f"Generation {state['generation']} running.")
                return 0
            ui = read_json(state_dir / "ui-state.json") or {}
            if ui.get("mode") == "agent" and ui.get("sampled_at", 0) > state["updated_at"]:
                print(f"Generation {state['generation']} running; agent has control.")
                return 0
        time.sleep(0.2)
    print("Timed out waiting for the development journal (is NX busy or showing a dialog?).")
    return 2


if __name__ == "__main__":
    sys.exit(main())

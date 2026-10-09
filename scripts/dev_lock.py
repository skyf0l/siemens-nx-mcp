"""Serialize live NX sessions between parallel development agents.

There is one NX. A dev reload restarts the host for everyone and resets object
IDs, so only the lock owner may run dev_reload.py, dev_python.py or the acceptance
runner (`python -m nx_mcp.acceptance run|call`, which takes the lock itself).

    python scripts/dev_lock.py acquire --owner holes      # waits; exit 0 = yours
    python scripts/dev_lock.py release --owner holes
    python scripts/dev_lock.py status

Re-acquiring as the current owner refreshes the lock. A lock not refreshed for
--stale minutes (default 20) is taken over, so keep live sessions short.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def lock_path(workspace: str) -> Path:
    return Path(workspace) / ".nx-mcp" / "dev.lock"


def read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def try_acquire(path: Path, owner: str, stale_seconds: float) -> bool:
    now = time.time()
    current = read(path)
    if current and current.get("owner") == owner:
        path.write_text(json.dumps({**current, "refreshed_at": now}), encoding="utf-8")
        return True
    if current and now - current.get("refreshed_at", 0) > stale_seconds:
        path.unlink(missing_ok=True)  # Abandoned by a crashed or forgetful agent.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"owner": owner, "acquired_at": now, "refreshed_at": now}, stream)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["acquire", "release", "status"])
    parser.add_argument("--owner")
    parser.add_argument("--wait", type=float, default=3600, help="seconds to wait for the lock")
    parser.add_argument(
        "--stale", type=float, default=20, help="minutes before a lock is abandoned"
    )
    parser.add_argument("--workspace", default=os.environ.get("NX_MCP_WORKSPACE"))
    args = parser.parse_args()
    if not args.workspace:
        parser.error("set NX_MCP_WORKSPACE or pass --workspace")
    path = lock_path(args.workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    if args.action == "status":
        current = read(path)
        if not current:
            print("free")
        else:
            age = time.time() - current.get("refreshed_at", 0)
            print(f"held by {current.get('owner')} (refreshed {age:.0f} s ago)")
        return 0
    if not args.owner:
        parser.error("--owner is required")
    if args.action == "release":
        current = read(path)
        if current and current.get("owner") == args.owner:
            path.unlink(missing_ok=True)
            print("released")
            return 0
        print(f"not held by {args.owner}: {current}")
        return 1
    deadline = time.monotonic() + args.wait
    while True:
        if try_acquire(path, args.owner, args.stale * 60):
            print(f"acquired by {args.owner}")
            return 0
        if time.monotonic() > deadline:
            print(f"timed out; {read(path)}")
            return 2
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())

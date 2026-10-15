"""Start graphical NX 2406 detached, optionally with a user directory for startup apps.

Development helper for fully automatic live testing: NX loads applications
from <UGII_USER_DIR>/startup at launch, so no journal has to be played by hand.

    python scripts/dev_start_nx.py --user-dir C:\\path\\to\\nx_user_dir
    python scripts/dev_start_nx.py --wait 120   # wait until NX shows its main window

NX inherits this environment: NX_MCP_WORKSPACE if set (else the hosts use
<home>/NX_MCP_WORKSPACE, like the sidecar); --env NX_MCP_WORKSPACE=... overrides it.
Without --user-dir no startup app loads; play nx_app/journals/start_2406.py instead.
NX runs in <workspace>/.nx-mcp/nx-cwd: it writes scratch files to its working directory
(udf_database.txt and dbc_udf_ascii.def when the hole and thread library loads), which
then stay out of the checkout.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def nx_pids() -> list[int]:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq ugraf.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [int(line.split(",")[1].strip('"')) for line in out.splitlines() if "ugraf.exe" in line]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--user-dir", type=Path, help="UGII_USER_DIR for this NX session")
    parser.add_argument("--env", action="append", default=[], help="extra NAME=VALUE")
    parser.add_argument("--wait", type=float, default=90, help="seconds to wait for the NX process")
    args = parser.parse_args()
    if nx_pids():
        print(f"NX is already running: {nx_pids()}")
        return 1
    base = Path(os.environ.get("UGII_BASE_DIR", r"C:\Program Files\Siemens\NX2406"))
    env = dict(os.environ)
    if args.user_dir:
        env["UGII_USER_DIR"] = str(args.user_dir.resolve())
    for item in args.env:
        name, _, value = item.partition("=")
        env[name] = value
    workspace = Path(env.get("NX_MCP_WORKSPACE") or Path.home() / "NX_MCP_WORKSPACE")
    cwd = workspace / ".nx-mcp" / "nx-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(base / "NXBIN" / "ugraf.exe")],
        env=env,
        cwd=cwd,
        creationflags=flags,
        close_fds=True,
    )
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if nx_pids():
            print(
                f"NX started: pid {nx_pids()[0]}"
                + (f", UGII_USER_DIR={env['UGII_USER_DIR']}" if args.user_dir else "")
            )
            return 0
        time.sleep(1)
    print("NX did not start in time")
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""``python -m nx_mcp.acceptance.sidecar_launch <caller>``: the stdio MCP server
(``python -m nx_mcp.server``) presenting the fixed caller id ``<caller>`` to NX.

A plain sidecar names itself by a random process id, so every ``acceptance call``
(a fresh sidecar each) would be another caller to the host's part ownership
(``core/busy.py``). The runner starts this one with ``runner.caller_id(owner)``:
calls and runs with the same ``--owner`` are one caller, and the parts one call
created are the owner's own in the next.
"""

from __future__ import annotations

import re
import sys

CALLER = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not CALLER.fullmatch(args[0]):
        raise SystemExit("usage: python -m nx_mcp.acceptance.sidecar_launch <caller id>")
    from nx_mcp import server
    from nx_mcp.sidecar import proxy

    proxy.PROCESS_ID = args[0]  # read at every call: proxy.caller_id and the bridge send
    server.main()


if __name__ == "__main__":
    main()

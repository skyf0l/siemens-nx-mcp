"""The number of tools the full profile lists in development mode (``NX_MCP_DEV=1``).

The gateways plus every catalogue tool of this checkout; release suites load this
file by path (``runpy``) so one count serves them all.
"""

from __future__ import annotations

import sys
from pathlib import Path


def count() -> int:
    src = str(Path(__file__).resolve().parents[1] / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from nx_mcp.core import catalogue
    from nx_mcp.sidecar import gateways

    return len(gateways.NAMES) + len(catalogue.load())


if __name__ == "__main__":
    print(count())

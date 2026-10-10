"""Play once in the open NX UI. Returns immediately; NX retains the UI-thread host."""

import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(root))
from nx_mcp.interactive import start  # noqa: E402 - NX journal loads this checkout first

workspace = os.environ.get("NX_MCP_WORKSPACE")
if not workspace:
    raise RuntimeError("Set NX_MCP_WORKSPACE before starting the NX MCP host")
descriptor = os.environ.get(
    "NX_MCP_UI_DESCRIPTOR",
    str(Path(os.environ["LOCALAPPDATA"]) / "nx-mcp" / "interactive-bridge.json"),
)
start(workspace, descriptor)

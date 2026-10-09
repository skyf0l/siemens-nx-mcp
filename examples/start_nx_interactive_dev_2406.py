"""DEPRECATED - can freeze NX. Use the startup application nx_app/startup/nx_mcp_startup.py
(see INTERACTIVE-NX.md). A resident journal holds NX's main loop, so NX work that needs it
(the first part of a fresh session, the first PDF export) waits forever.

DEVELOPMENT ONLY, NX 2406. Play once in graphical NX (Alt+F8) on a development machine.

Like start_nx_interactive_2406.py, but the agent gets control immediately (no
click) and the host reloads from disk whenever <workspace>/.nx-mcp/dev-restart
appears (scripts/dev_reload.py creates it and waits for the result). Take
control back from the panel at any time; restarts then keep you in control.
Never use this launcher for production work. See nx_mcp/devloop.py.
"""

import os
import sys
import traceback
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(root))

workspace = os.environ.get("NX_MCP_WORKSPACE", r"D:\CAD\NX_MCP_WORKSPACE")
descriptor = os.environ.get(
    "NX_MCP_UI_DESCRIPTOR",
    str(Path(os.environ["LOCALAPPDATA"]) / "nx-mcp" / "interactive-bridge.json"),
)
try:
    from nx_mcp.devloop import run_dev

    run_dev(workspace, descriptor)
except BaseException:
    error_file = Path(workspace) / ".nx-mcp" / "resident-error.txt"
    error_file.parent.mkdir(parents=True, exist_ok=True)
    error_file.write_text(traceback.format_exc())
    raise

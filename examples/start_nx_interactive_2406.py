"""Play once in the open NX UI on NX 2406 and other releases that tear down a
journal's interpreter when it returns. The journal stays resident, pumping NX
messages and draining bridge calls, until the panel's Stop button or the stop
file ends it; NX is then back to normal. It starts in manual mode.
"""

import os
import sys
import traceback
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(root))
from nx_mcp.interactive import run_resident  # noqa: E402 - NX journal loads this checkout first

workspace = os.environ.get("NX_MCP_WORKSPACE", r"D:\CAD\NX_MCP_WORKSPACE")
descriptor = os.environ.get(
    "NX_MCP_UI_DESCRIPTOR",
    str(Path(os.environ["LOCALAPPDATA"]) / "nx-mcp" / "interactive-bridge.json"),
)
try:
    run_resident(workspace, descriptor)
except BaseException:
    error_file = Path(workspace) / ".nx-mcp" / "resident-error.txt"
    error_file.parent.mkdir(parents=True, exist_ok=True)
    error_file.write_text(traceback.format_exc())
    raise

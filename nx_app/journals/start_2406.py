# nx: threaded
"""Play in graphical NX 2406 (Alt+F8, Run) to start the NX MCP bridge host and panel.

For an NX session started without nx_app on UGII_USER_DIR. Line 1 makes NX run
this journal in its main Python interpreter, which stays loaded after the journal
returns, so the bridge keeps running and NX's main loop is never held. The host
code is the startup application's (nx_app/startup/nx_mcp_startup.py, play()):
when the startup app or an earlier play already runs the bridge in this NX, this
only reports it and shows the panel. NX_MCP_WORKSPACE overrides the default
workspace <home>/NX_MCP_WORKSPACE.
"""

import importlib.util
import sys
from pathlib import Path

NAME = "nx_mcp_startup"  # The bootstrap registers itself under this name.
BOOTSTRAP = Path(__file__).resolve().parents[1] / "startup" / "nx_mcp_startup.py"


def bootstrap():
    """The bootstrap already loaded in this NX, else a fresh one from the checkout."""
    module = sys.modules.get(NAME)
    if module is None:
        spec = importlib.util.spec_from_file_location(NAME, BOOTSTRAP)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load the NX MCP bootstrap {BOOTSTRAP}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[NAME] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[NAME]
            raise
    return module


print(bootstrap().play())

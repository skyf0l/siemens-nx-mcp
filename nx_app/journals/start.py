# nx: threaded
"""Play in graphical NX (Alt+F8, Run) to start the NX MCP bridge host and panel.

Works on any NX build. It first prints the NX build (and whether NX MCP was tested
on it), NX's Python version and which NXOpen namespaces import (a missing one is
reported, not fatal: only the tools that need it fail). NX's Python must be 3.10 or
newer; an older one is refused with a clear message.

For an NX session started without nx_app on UGII_USER_DIR. Line 1 makes NX run
this journal in its main Python interpreter, which stays loaded after the journal
returns, so the bridge keeps running and NX's main loop is never held. The host
code is the startup application's (nx_app/startup/nx_mcp_startup.py, play()):
when the startup app or an earlier play already runs the bridge in this NX, from
this interpreter or another one, this only reports it and shows the panel; a
second bridge never starts in one NX process. NX_MCP_WORKSPACE overrides the
default workspace <home>/NX_MCP_WORKSPACE.
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
            raise RuntimeError("Cannot load the NX MCP bootstrap " + str(BOOTSTRAP))
        module = importlib.util.module_from_spec(spec)
        sys.modules[NAME] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[NAME]
            raise
    return module


def main():
    try:
        module = bootstrap()
    except ImportError as exc:  # e.g. ctypes in an isolated sub-interpreter
        raise RuntimeError(
            "NX ran this journal in a Python interpreter that cannot host NX MCP ("
            + str(exc)
            + "); play it with '# nx: threaded' as its first line"
        ) from exc
    # A bootstrap NX loaded before this checkout was updated may lack these.
    problem = getattr(module, "python_problem", lambda: None)()
    if problem:
        print(problem)
        raise RuntimeError(problem)
    for line in getattr(module, "environment", list)():
        print(line)
    print(module.play())


main()

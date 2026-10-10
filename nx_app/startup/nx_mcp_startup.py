# nx: threaded
"""NX MCP bridge bootstrap for NX 2406: a startup application and a playable journal.

NX loads this file at launch from <dir>/startup for every <dir> it scans for
applications (UGII_USER_DIR, UGII_SITE_DIR, or an entry in custom_dirs.dat) and
calls startup(). nx_app/journals/start_2406.py, played in any graphical NX,
imports this file and calls play() instead. The "# nx: threaded" flag on line 1
(of this file and of the journal) makes NX run the code in its main Python
interpreter, which stays loaded for the whole NX session; a journal without it
runs in a sub-interpreter that NX unloads when the journal returns. A timer on a
hidden window ticks the bridge host on NX's UI thread between NX commands, so
NX's main loop is never blocked: no journal keeps running, there is no "Work in
Progress" window, and first-time NX work (the Modeling application, the PDF
exporter) cannot deadlock. One bootstrap per NX process: both entry points share
sys.modules["nx_mcp_startup"], so a second play only reports the running host.

Environment:
  NX_MCP_WORKSPACE     workspace shared with the MCP sidecar (default <home>/NX_MCP_WORKSPACE,
                       the sidecar's default too: nx_mcp.workspace.default_workspace)
  NX_MCP_UI_DESCRIPTOR bridge descriptor path (default %LOCALAPPDATA%/nx-mcp/interactive-bridge.json)
  NX_MCP_SRC           nx_mcp source root (default: ../../src next to this folder)
  NX_MCP_DEV=1         development behaviour: the panel says "NX MCP DEV", the host
                       reloads from disk on <workspace>/.nx-mcp/dev-restart
                       (scripts/dev_reload.py) and nx_dev_python is available.
This module is the only code NX keeps loaded; nx_mcp is imported from disk by it.
"""

import contextlib
import ctypes
import importlib
import json
import os
import sys
import time
import traceback
import types
from ctypes import wintypes as w
from pathlib import Path

DEV = os.environ.get("NX_MCP_DEV") == "1"
SRC = Path(os.environ.get("NX_MCP_SRC") or Path(__file__).resolve().parents[2] / "src")
# nx_mcp.workspace.default_workspace(); nx_mcp is not importable yet here.
WORKSPACE = os.environ.get("NX_MCP_WORKSPACE") or str(Path.home() / "NX_MCP_WORKSPACE")
DESCRIPTOR = os.environ.get(
    "NX_MCP_UI_DESCRIPTOR",
    str(Path(os.environ.get("LOCALAPPDATA", ".")) / "nx-mcp" / "interactive-bridge.json"),
)
STATE_DIR = Path(WORKSPACE) / ".nx-mcp"
ERROR_FILE = STATE_DIR / "resident-error.txt"
STATE_FILE = STATE_DIR / "dev-state.json"
RESTART_FILE = STATE_DIR / "dev-restart"
TICK_MS = 50
RESTART_ATTEMPTS = 5  # Per reload request; then the request is dropped.
RESTART_BACKOFF_MAX = 8.0  # Seconds between attempts after a failed restart.
MODULE_NAME = "nx_mcp_startup"  # Where a played journal finds the running bootstrap.

state = {
    "restart_failures": 0,
    "restart_after": 0.0,
    "restart_error": None,
    "armed": False,
    "host": None,
    "generation": 0,
    "retired": [],  # Old hosts keep native callback delegates alive until NX exits.
    "keep": [],  # This module's own native delegates.
    "busy": False,
    "loader": None,  # "startup app" or "journal": what armed this bootstrap.
}

user32 = ctypes.WinDLL("user32", use_last_error=True)
LONG_PTR = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LONG_PTR, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
ENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = LONG_PTR
user32.CreateWindowExW.argtypes = [
    w.DWORD,
    w.LPCWSTR,
    w.LPCWSTR,
    w.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    w.HWND,
    w.HMENU,
    w.HINSTANCE,
    ctypes.c_void_p,
]
user32.CreateWindowExW.restype = w.HWND
user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [w.HWND, ctypes.c_size_t]
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.IsWindowVisible.argtypes = [w.HWND]
user32.FindWindowExW.argtypes = [w.HWND, w.HWND, w.LPCWSTR, w.LPCWSTR]
user32.FindWindowExW.restype = w.HWND
user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [
    w.HWND,
    w.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    w.UINT,
]
WM_TIMER = 0x0113
HWND_MESSAGE = w.HWND(-3)
SW_SHOWNA = 8
SWP_SHOW_ON_TOP = 0x0001 | 0x0002 | 0x0010 | 0x0040  # NOSIZE|NOMOVE|NOACTIVATE|SHOWWINDOW


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", w.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HICON),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
    ]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]


def write_state(**fields):
    with contextlib.suppress(OSError):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "generation": state["generation"],
            "updated_at": time.time(),
            "driver": "startup",
        }
        payload.update(fields)
        temp = STATE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(payload), encoding="utf-8")
        temp.replace(STATE_FILE)


def process_windows(visible=True):
    """Top-level windows of this NX process as (hwnd, class name)."""
    found = []

    def visit(hwnd, _):
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid() and (not visible or user32.IsWindowVisible(hwnd)):
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, 256)
            found.append((hwnd, name.value))
        return True

    callback = ENUMPROC(visit)
    user32.EnumWindows(callback, 0)
    return found


def nx_has_main_window():
    return bool(process_windows())


def window_class():
    return f"NxMcpStartup_{os.getpid()}"


def foreign_bootstrap_window():
    """True when another copy of this bootstrap (one this state does not know) armed a timer."""
    hwnd = user32.FindWindowExW(HWND_MESSAGE, None, window_class(), None)
    return bool(hwnd) and hwnd != state.get("hwnd")


def panel_windows():
    return [hwnd for hwnd, name in process_windows(visible=False) if name.startswith("NxMcpPanel")]


def show_window(hwnd):
    """Show a panel above NX's windows without taking the focus from NX."""
    user32.ShowWindow(hwnd, SW_SHOWNA)
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_SHOW_ON_TOP)


def purge_nx_mcp():
    for name in list(sys.modules):
        if name == "nx_mcp" or name.startswith("nx_mcp."):
            del sys.modules[name]
    importlib.invalidate_caches()


def start_host():
    """(Re)import nx_mcp from disk and start a host; failures are reported, never raised."""
    state["generation"] += 1
    RESTART_FILE.unlink(missing_ok=True)
    try:
        purge_nx_mcp()
        if str(SRC) not in sys.path:
            sys.path.insert(0, str(SRC))
        interactive = importlib.import_module("nx_mcp.interactive")
        missing = interactive.import_nxopen_namespaces()
        interactive.start(
            WORKSPACE,
            DESCRIPTOR,
            resident=True,  # Ticked by this module, not by a timer of its own.
            dev=DEV,
            driver="startup",
        )
        host = interactive._host
        state["host"] = host
        state["retired"].append(host)
        if missing:
            host.last_error = "NXOpen namespaces not importable: " + "; ".join(missing)
        ERROR_FILE.unlink(missing_ok=True)
        write_state(running=True, ok=True, error=None)
    except Exception as exc:
        state["host"] = None
        with contextlib.suppress(OSError):
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            ERROR_FILE.write_text(traceback.format_exc(), encoding="utf-8")
        write_state(running=True, ok=False, error=str(exc))


def restart_host():
    host = state["host"]
    if host is not None and not host.stopped:
        host.stop()  # The new host keeps the panel mode (ui-mode.json).
    start_host()


def restart_failed(exc):
    """Back off after a failed restart; record each distinct error once."""
    state["restart_failures"] += 1
    message = f"restart: {type(exc).__name__}: {exc}"
    if message != state["restart_error"]:
        state["restart_error"] = message
        with contextlib.suppress(OSError):
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            ERROR_FILE.write_text(message + "\n" + traceback.format_exc(), encoding="utf-8")
    if state["restart_failures"] >= RESTART_ATTEMPTS:
        # Give up on this request; scripts/dev_reload.py sees a failed generation.
        RESTART_FILE.unlink(missing_ok=True)
        state["generation"] += 1
        write_state(running=True, ok=False, error=message)
        state["restart_failures"], state["restart_after"] = 0, 0.0
        state["restart_error"] = None
        return
    delay = min(RESTART_BACKOFF_MAX, TICK_MS / 1000 * 2 ** (state["restart_failures"] + 2))
    state["restart_after"] = time.monotonic() + delay


def tick():
    if state["busy"]:
        return
    state["busy"] = True
    try:
        host = state["host"]
        if host is None and state["generation"] == 0:
            # NX calls startup() before its main window exists; the host needs it.
            if nx_has_main_window():
                start_host()
            return
        if DEV and RESTART_FILE.exists() and time.monotonic() >= state["restart_after"]:
            try:
                restart_host()
                state["restart_failures"], state["restart_error"] = 0, None
            except Exception as exc:
                restart_failed(exc)
            return
        # A backed-off restart keeps ticking the current host meanwhile.
        if host is not None and not host.stopped:
            host.tick()
        elif host is not None and host.stopped and DEV:
            write_state(running=False, ok=True, error=None)
    except BaseException as exc:  # Never let an exception reach NX's message loop.
        with contextlib.suppress(OSError):
            ERROR_FILE.write_text(f"tick: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    finally:
        state["busy"] = False


def window_proc(hwnd, message, wparam, lparam):
    if message == WM_TIMER:
        tick()
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


def arm(loader):
    if state["armed"]:
        return
    state["armed"] = True
    state["loader"] = loader
    proc = WNDPROC(window_proc)
    cls = WNDCLASS()
    cls.lpfnWndProc = proc
    cls.lpszClassName = window_class()
    state["keep"].extend([proc, cls])
    user32.RegisterClassW(ctypes.byref(cls))
    hwnd = user32.CreateWindowExW(
        0, cls.lpszClassName, "nx-mcp", 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None
    )
    state["hwnd"] = hwnd
    user32.SetTimer(hwnd, 1, TICK_MS, None)
    write_state(running=True, ok=True, error=None, armed=True)


def register():
    """Make this bootstrap sys.modules["nx_mcp_startup"], whatever name NX loaded it under.

    A played start_2406.py runs in the same (main) interpreter and imports that
    name, so it finds the running bootstrap instead of starting a second bridge.
    The alias shares this module's functions and state."""
    if __name__ == MODULE_NAME or MODULE_NAME in sys.modules:
        return
    alias = types.ModuleType(MODULE_NAME)
    alias.__dict__.update(globals())
    sys.modules[MODULE_NAME] = alias


def startup(*_):
    """Entry point NX calls for applications in a startup folder."""
    try:
        register()
        arm("startup app")
    except BaseException:
        with contextlib.suppress(OSError):
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            ERROR_FILE.write_text("startup: " + traceback.format_exc(), encoding="utf-8")
    return 0


def running_host():
    host = state["host"]
    return host if host is not None and not host.stopped else None


def already_running(host):
    """Say so on the panel and bring the panel up; returns the journal's report."""
    message = f"NX MCP bridge is already running in this NX (started by the {state['loader']})."
    with contextlib.suppress(Exception):
        host._set_notice(message)
    with contextlib.suppress(Exception):
        show_window(host.panel.hwnd)
    return message


def play():
    """Entry point of the played journal nx_app/journals/start_2406.py.

    Starts the bridge host and panel now (NX's main window exists when a journal
    is played) and arms the timer that ticks it after the journal returns. When
    a host already runs in this NX, from the startup app or an earlier play, it
    only reports that and shows the panel. A host stopped from its panel is
    started again. Returns the line the journal prints; raises RuntimeError when
    the host cannot start (traceback in <workspace>/.nx-mcp/resident-error.txt)."""
    host = running_host()
    if host is not None:
        return already_running(host)
    if not state["armed"] and foreign_bootstrap_window():
        # A bootstrap this module cannot reach (another interpreter) owns the bridge.
        for hwnd in panel_windows():
            show_window(hwnd)
        return "NX MCP bridge is already running in this NX (started by another bootstrap)."
    arm("journal")
    start_host()
    if running_host() is None:
        detail = ERROR_FILE.read_text(encoding="utf-8") if ERROR_FILE.exists() else ""
        raise RuntimeError(f"NX MCP bridge did not start; see {ERROR_FILE}\n{detail}")
    return f"NX MCP bridge started for workspace {WORKSPACE}; see the NX MCP control panel."

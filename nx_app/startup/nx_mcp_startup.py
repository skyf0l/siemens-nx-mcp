# nx: threaded
"""NX MCP bridge as an NX startup application (NX 2406; replaces played journals).

NX loads this file at launch from <dir>/startup for every <dir> it scans for
applications (UGII_USER_DIR, UGII_SITE_DIR, or an entry in custom_dirs.dat) and
calls startup(). The "# nx: threaded" flag on line 1 keeps the module loaded for
the whole NX session. A timer on a hidden window ticks the bridge host on NX's
UI thread between NX commands, so NX's main loop is never blocked: no journal
is played, there is no "Work in Progress" window, and first-time NX work (the
Modeling application, the PDF exporter) cannot deadlock.

Environment:
  NX_MCP_WORKSPACE     workspace shared with the MCP sidecar (required)
  NX_MCP_UI_DESCRIPTOR bridge descriptor path (default %LOCALAPPDATA%/nx-mcp/interactive-bridge.json)
  NX_MCP_SRC           nx_mcp source root (default: ../../src next to this folder)
  NX_MCP_DEV=1         development behaviour: the agent gets control without a
                       click, the host reloads from disk on <workspace>/.nx-mcp/dev-restart
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
from ctypes import wintypes as w
from pathlib import Path

DEV = os.environ.get("NX_MCP_DEV") == "1"
SRC = Path(os.environ.get("NX_MCP_SRC") or Path(__file__).resolve().parents[2] / "src")
WORKSPACE = os.environ.get("NX_MCP_WORKSPACE", r"D:\CAD\NX_MCP_WORKSPACE")
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

state = {
    "restart_failures": 0,
    "restart_after": 0.0,
    "restart_error": None,
    "armed": False,
    "host": None,
    "generation": 0,
    "agent_control": DEV,
    "retired": [],  # Old hosts keep native callback delegates alive until NX exits.
    "keep": [],  # This module's own native delegates.
    "busy": False,
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
WM_TIMER = 0x0113
HWND_MESSAGE = w.HWND(-3)


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


def nx_has_main_window():
    found = []

    def visit(hwnd, _):
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid() and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    callback = ENUMPROC(visit)
    user32.EnumWindows(callback, 0)
    return bool(found)


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
            agent_control=state["agent_control"],
            dev=DEV,
            driver="startup",
        )
        host = interactive._host
        state["host"] = host
        state["retired"].append(host)
        if missing:
            host.last_error = "NXOpen namespaces not importable: " + "; ".join(missing)
        ERROR_FILE.unlink(missing_ok=True)
        write_state(running=True, ok=True, agent_control=state["agent_control"], error=None)
    except Exception as exc:
        state["host"] = None
        with contextlib.suppress(OSError):
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            ERROR_FILE.write_text(traceback.format_exc(), encoding="utf-8")
        write_state(running=True, ok=False, agent_control=state["agent_control"], error=str(exc))


def restart_host():
    host = state["host"]
    if host is not None:
        # Keep the engineer's choice: after "Take control back" stay with them.
        state["agent_control"] = host.mode == "agent" or bool(getattr(host, "auto_start", False))
        if not host.stopped:
            host.stop()
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
        write_state(running=True, ok=False, agent_control=state["agent_control"], error=message)
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


def arm():
    if state["armed"]:
        return
    state["armed"] = True
    proc = WNDPROC(window_proc)
    cls = WNDCLASS()
    cls.lpfnWndProc = proc
    cls.lpszClassName = f"NxMcpStartup_{os.getpid()}"
    state["keep"].extend([proc, cls])
    user32.RegisterClassW(ctypes.byref(cls))
    hwnd = user32.CreateWindowExW(
        0, cls.lpszClassName, "nx-mcp", 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None
    )
    state["hwnd"] = hwnd
    user32.SetTimer(hwnd, 1, TICK_MS, None)
    write_state(running=True, ok=True, agent_control=state["agent_control"], error=None, armed=True)


def startup(*_):
    """Entry point NX calls for applications in a startup folder."""
    try:
        arm()
    except BaseException:
        with contextlib.suppress(OSError):
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            ERROR_FILE.write_text("startup: " + traceback.format_exc(), encoding="utf-8")
    return 0

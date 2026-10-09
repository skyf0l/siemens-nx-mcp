"""NX 2406 experiment 5: a Python startup application that stays loaded.

Loaded by NX at launch from <UGII_USER_DIR>/startup (scripts/dev_start_nx.py
--user-dir scripts/nx_experiments/nx_user_dir). Logs to
<workspace>/.nx-mcp/exp5.log:
- whether NX imports this file at all, and which entry point it calls
  (startup / Startup / main / ufsta);
- which thread runs it (vs. the thread owning NX's main window);
- which tick source keeps firing during the session: a thread timer and a
  timer on a hidden window of our own;
- whether the first part of a fresh session (the call that froze NX under the
  resident journal host) can be created from such a tick.
Stops ticking after ~90 s.
"""

import ctypes
import os
import sys
import time
from ctypes import wintypes as w
from pathlib import Path

WORKSPACE = Path(os.environ.get("NX_MCP_WORKSPACE", r"D:\CAD\NX_MCP_WORKSPACE"))
LOG = WORKSPACE / ".nx-mcp" / "exp5.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
START = time.time()


def log(message):
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"{time.strftime('%H:%M:%S')} {time.time() - START:7.2f}s  {message}\n")


log(f"module imported: __name__={__name__} python={sys.version.split()[0]} pid={os.getpid()}")
sys.modules["nx_exp5_marker"] = type(sys)("nx_exp5_marker")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LONG_PTR = ctypes.c_ssize_t
TIMERPROC = ctypes.WINFUNCTYPE(None, w.HWND, w.UINT, ctypes.c_size_t, w.DWORD)
WNDPROC = ctypes.WINFUNCTYPE(LONG_PTR, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
ENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [w.HWND, ctypes.c_size_t]
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = LONG_PTR
user32.CreateWindowExW.argtypes = [
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = w.HWND
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetWindowThreadProcessId.restype = w.DWORD
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
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
STATE = {"armed": False, "thread_ticks": 0, "window_ticks": 0, "tested": False, "done": False}
KEEP = []  # Native delegates must stay referenced for the whole session.


def nx_main_window():
    windows = []

    def visit(hwnd, _):
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid() and user32.IsWindowVisible(hwnd):
            rect = w.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            windows.append(((rect.right - rect.left) * (rect.bottom - rect.top), hwnd))
        return True

    callback = ENUMPROC(visit)
    user32.EnumWindows(callback, 0)
    return max(windows)[1] if windows else None


def first_part_test(source):
    import NXOpen

    session = NXOpen.Session.GetSession()
    parts = sum(1 for _ in session.Parts)
    log(f"{source}: first-part test, parts={parts} app={session.ApplicationName}")
    if parts:
        log(f"{source}: a part is already open; skipping the fresh-session test")
        return
    path = WORKSPACE / "smoke" / "experiments" / f"exp5_{time.strftime('%H%M%S')}.prt"
    path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    builder = session.Parts.FileNew()
    try:
        builder.TemplateFileName = "model-plain-1-mm-template.prt"
        builder.Units = NXOpen.Part.Units.Millimeters
        builder.NewFileName = str(path)
        builder.DisplayPartOption = NXOpen.DisplayPartOption.AllowAdditional
        builder.Commit()
    finally:
        builder.Destroy()
    log(f"{source}: FIRST PART CREATED in {time.time() - t0:.2f}s -> {path.name}, app now {session.ApplicationName}")


def on_tick(source):
    key = source + "_ticks"
    STATE[key] += 1
    n = STATE[key]
    if n in (1, 2, 5) or n % 40 == 0:
        main = nx_main_window()
        ui = user32.GetWindowThreadProcessId(main, None) if main else None
        log(
            f"{source} tick {n}: thread {kernel32.GetCurrentThreadId()} (NX UI thread {ui}), "
            f"marker visible={'nx_exp5_marker' in sys.modules}"
        )
    # After ~10 s, with NX's main window up, try the call that used to freeze.
    if n == 20 and not STATE["tested"] and nx_main_window():
        STATE["tested"] = True
        try:
            first_part_test(source)
        except BaseException as error:
            log(f"{source}: first-part test ERROR {type(error).__name__}: {error}")
    if n >= 180 and not STATE["done"]:
        STATE["done"] = True
        log(f"{source}: stopping ticks after {n}; experiment finished")
        return False
    return True


def thread_timer(hwnd, message, timer_id, elapsed):
    try:
        if not on_tick("thread"):
            user32.KillTimer(None, timer_id)
    except BaseException as error:
        log(f"thread tick ERROR {type(error).__name__}: {error}")


def window_proc(hwnd, message, wparam, lparam):
    if message == WM_TIMER:
        try:
            if not on_tick("window"):
                user32.KillTimer(hwnd, 1)
        except BaseException as error:
            log(f"window tick ERROR {type(error).__name__}: {error}")
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


def arm(entry_point):
    log(f"entry point called: {entry_point} on thread {kernel32.GetCurrentThreadId()}")
    if STATE["armed"]:
        return 0
    STATE["armed"] = True
    timer_cb = TIMERPROC(thread_timer)
    KEEP.append(timer_cb)
    tid = user32.SetTimer(None, 0, 500, ctypes.cast(timer_cb, ctypes.c_void_p))
    proc = WNDPROC(window_proc)
    KEEP.append(proc)
    cls = WNDCLASS()
    cls.lpfnWndProc = proc
    cls.lpszClassName = f"NxMcpExp5_{os.getpid()}"
    KEEP.append(cls)
    user32.RegisterClassW(ctypes.byref(cls))
    hwnd = user32.CreateWindowExW(0, cls.lpszClassName, "nx-mcp exp5", 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
    wid = user32.SetTimer(hwnd, 1, 500, None)
    log(f"armed: thread timer {tid}, hidden window {hwnd} timer {wid}, error {ctypes.get_last_error()}")
    return 0


def GetUnloadOption(*_):  # noqa: N802 - NX convention
    import NXOpen

    log("GetUnloadOption called")
    return NXOpen.Session.LibraryUnloadOption.AtTermination


def startup(*_):
    return arm("startup")


def Startup(*_):  # noqa: N802 - .NET-style name, in case NX uses it for Python
    return arm("Startup")


def ufsta(*_):
    return arm("ufsta")


def main(*_):
    return arm("main")


if __name__ == "__main__":
    arm("__main__")

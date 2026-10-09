"""NX 2406 experiment 3: what can call back into a journal after it returns?

Experiment 2 (GetUnloadOption = AtTermination enum, thread timer, no thread):
NX stayed responsive but no timer tick ever ran after the journal returned.
NX 2406's UI runs on Qt, which apparently does not deliver bare thread timers.

This journal arms two independent callback routes and returns at once:
(a) a hidden message-only window of our own with a timer bound to it - window
    messages are dispatched by any event loop (Qt's included);
(b) an NX-native "work part changed" handler, called by NX itself.
At tick 10 the window timer creates the first part of the session (the call
that froze NX under the resident launcher); that also fires (b).
Everything is logged to <workspace>/.nx-mcp/exp3.log. Stops after ~40 s.
Play in a fresh NX with no part open.
"""

import ctypes
import os
import sys
import time
from ctypes import wintypes as w
from pathlib import Path

import NXOpen

WORKSPACE = Path(os.environ.get("NX_MCP_WORKSPACE", r"D:\CAD\NX_MCP_WORKSPACE"))
LOG = WORKSPACE / ".nx-mcp" / "exp3.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
LOG.write_text("", encoding="utf-8")
START = time.time()


def log(message):
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"{time.time() - START:7.2f}s  {message}\n")


def GetUnloadOption(dummy):  # noqa: N802 - NX looks this name up after the journal returns.
    return NXOpen.Session.LibraryUnloadOption.AtTermination


sys.modules["nx_exp3_marker"] = type(sys)("nx_exp3_marker")
session = NXOpen.Session.GetSession()
log(f"journal start: parts={sum(1 for _ in session.Parts)} app={session.ApplicationName}")
state = {"ticks": 0, "created": False}


def create_first_part():
    path = WORKSPACE / "smoke" / "experiments" / f"exp3_{time.strftime('%H%M%S')}.prt"
    path.parent.mkdir(parents=True, exist_ok=True)
    log(f"creating first part {path.name} (app={session.ApplicationName})")
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
    log(f"part created in {time.time() - t0:.2f}s, app now {session.ApplicationName}")


# ---- (a) hidden message-only window with a window-bound timer ----------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


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


user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.CreateWindowExW.argtypes = [
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = w.HWND
user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [w.HWND, ctypes.c_size_t]
WM_TIMER = 0x0113
HWND_MESSAGE = w.HWND(-3)


def window_proc(hwnd, message, wparam, lparam):
    if message == WM_TIMER:
        try:
            state["ticks"] += 1
            n = state["ticks"]
            if n in (1, 2, 5) or n % 20 == 0:
                import sys as s  # Resolved by whichever interpreter runs this.

                log(
                    f"window tick {n}: marker visible={'nx_exp3_marker' in s.modules} "
                    f"parts={sum(1 for _ in session.Parts)} app={session.ApplicationName}"
                )
            if n == 10 and not state["created"]:
                state["created"] = True
                create_first_part()
            if n >= 80:
                user32.KillTimer(hwnd, 1)
                log(f"window tick {n}: timer stopped; experiment finished")
        except BaseException as error:
            import traceback

            log(f"window tick ERROR {type(error).__name__}: {error}\n{traceback.format_exc()}")
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


proc = WNDPROC(window_proc)
cls = WNDCLASS()
cls.lpfnWndProc = proc
cls.lpszClassName = f"NxMcpExp3_{os.getpid()}_{int(START)}"
atom = user32.RegisterClassW(ctypes.byref(cls))
hwnd = user32.CreateWindowExW(0, cls.lpszClassName, "nx-mcp exp3", 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
timer = user32.SetTimer(hwnd, 1, 500, None)
log(f"journal: class atom {atom}, hwnd {hwnd}, window timer {timer}, error {ctypes.get_last_error()}")

# ---- (b) NX-native callback ----------------------------------------------


def work_part_changed(new_part):
    try:
        log(f"NX callback: work part changed -> {new_part.Name if new_part else None}")
    except BaseException as error:
        log(f"NX callback ERROR {type(error).__name__}: {error}")


try:
    handler_id = session.Parts.AddWorkPartChangedHandler(work_part_changed)
    log(f"journal: work-part-changed handler id {handler_id}")
except Exception as error:
    log(f"journal: cannot register NX handler: {type(error).__name__}: {error}")

KEEP = (proc, cls, work_part_changed)  # Keep native delegates referenced.
log("journal: returning now")

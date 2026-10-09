"""NX 2406 experiment 4: does the journal run off NX's UI thread, and can we run
work ON the UI thread after the journal returns?

Experiments 2-3: after the journal returned, neither a thread timer nor a
hidden window of our own ever received a message. Hypothesis: NX 2406 runs
Python journals on a worker thread whose message queue nobody pumps once the
journal returns; NX's UI thread waits for the journal while it runs (the
permanent "Work in Progress" dialog), which also explains the freezes when a
journal call needs the UI thread.

This journal:
1. logs the journal thread id and the thread id owning NX's main window;
2. subclasses NX's main window, so our window procedure runs on the UI thread;
3. posts one message to it; from the UI thread a timer on that window then
   ticks every 0.5 s, creates the first part of the session at tick 10, and
   removes the subclass again at tick 80 (~40 s).
Everything is logged to <workspace>/.nx-mcp/exp4.log.
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
LOG = WORKSPACE / ".nx-mcp" / "exp4.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
LOG.write_text("", encoding="utf-8")
START = time.time()


def log(message):
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"{time.time() - START:7.2f}s  {message}\n")


def GetUnloadOption(dummy):  # noqa: N802 - NX looks this name up after the journal returns.
    return NXOpen.Session.LibraryUnloadOption.AtTermination


sys.modules["nx_exp4_marker"] = type(sys)("nx_exp4_marker")
session = NXOpen.Session.GetSession()
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LONG_PTR = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LONG_PTR, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
ENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetWindowThreadProcessId.restype = w.DWORD
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.IsWindowVisible.argtypes = [w.HWND]
user32.SetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int, LONG_PTR]
user32.SetWindowLongPtrW.restype = LONG_PTR
user32.CallWindowProcW.argtypes = [LONG_PTR, w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.CallWindowProcW.restype = LONG_PTR
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [w.HWND, ctypes.c_size_t]
GWLP_WNDPROC = -4
WM_TIMER = 0x0113
WM_KICK = 0x8000 + 0x4E7  # WM_APP + private offset
TIMER_ID = 0x4E58


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

    user32.EnumWindows(ENUMPROC(visit), 0)
    return max(windows)[1]


main = nx_main_window()
ui_thread = user32.GetWindowThreadProcessId(main, None)
journal_thread = kernel32.GetCurrentThreadId()
log(
    f"journal thread {journal_thread}, NX main window {main} owned by thread {ui_thread} "
    f"-> journal {'IS' if journal_thread == ui_thread else 'is NOT'} on the UI thread"
)
log(f"journal: parts={sum(1 for _ in session.Parts)} app={session.ApplicationName}")
state = {"ticks": 0, "created": False, "old": None, "done": False}


def create_first_part():
    path = WORKSPACE / "smoke" / "experiments" / f"exp4_{time.strftime('%H%M%S')}.prt"
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


def tick(hwnd):
    state["ticks"] += 1
    n = state["ticks"]
    if n in (1, 2, 5) or n % 20 == 0:
        import sys as s  # Resolved by whichever interpreter runs this.

        log(
            f"UI tick {n}: thread {kernel32.GetCurrentThreadId()} marker visible="
            f"{'nx_exp4_marker' in s.modules} parts={sum(1 for _ in session.Parts)} "
            f"app={session.ApplicationName}"
        )
    if n == 10 and not state["created"]:
        state["created"] = True
        create_first_part()
    if n >= 80 and not state["done"]:
        state["done"] = True
        user32.KillTimer(hwnd, TIMER_ID)
        user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, state["old"])  # Unsubclass.
        log(f"UI tick {n}: timer stopped, NX window procedure restored; experiment finished")


def subclass_proc(hwnd, message, wparam, lparam):
    try:
        if message == WM_KICK:
            log(f"UI kick received on thread {kernel32.GetCurrentThreadId()}")
            timer = user32.SetTimer(hwnd, TIMER_ID, 500, None)
            log(f"UI timer {timer} (error {ctypes.get_last_error()})")
            return 0
        if message == WM_TIMER and wparam == TIMER_ID:
            tick(hwnd)
            return 0
    except BaseException as error:
        import traceback

        log(f"UI ERROR {type(error).__name__}: {error}\n{traceback.format_exc()}")
        return 0
    return user32.CallWindowProcW(state["old"], hwnd, message, wparam, lparam)


proc = WNDPROC(subclass_proc)
proc_address = ctypes.cast(proc, ctypes.c_void_p).value
state["old"] = user32.SetWindowLongPtrW(main, GWLP_WNDPROC, proc_address)
log(f"journal: subclassed main window (old proc {state['old']}, error {ctypes.get_last_error()})")
ok = user32.PostMessageW(main, WM_KICK, 0, 0)
log(f"journal: posted kick ({ok}); returning now")
KEEP = (proc, subclass_proc)  # Keep the native delegate referenced.

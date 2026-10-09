# 5. Cost of candidate "engineer is busy" checks on the NX UI thread.
import ctypes
import json
import os
import time
from ctypes import wintypes as w

import nx_mcp.interactive as I

h = I._host
ui = h.ui
S = h.nx.UI.Status
user = ctypes.WinDLL("user32", use_last_error=True)
pid = os.getpid()
tid = ctypes.windll.kernel32.GetCurrentThreadId()
N = BENCH_ARGS.get("n", 200)
pc = time.perf_counter
ENUM = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user.IsWindowVisible.argtypes = [w.HWND]
user.IsWindowEnabled.argtypes = [w.HWND]
user.GetWindow.argtypes = [w.HWND, w.UINT]
user.GetWindow.restype = w.HWND
user.GetAsyncKeyState.argtypes = [ctypes.c_int]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.DWORD),
        ("flags", w.DWORD),
        ("hwndActive", w.HWND),
        ("hwndFocus", w.HWND),
        ("hwndCapture", w.HWND),
        ("hwndMenuOwner", w.HWND),
        ("hwndMoveSize", w.HWND),
        ("hwndCaret", w.HWND),
        ("rcCaret", w.RECT),
    ]


def visible_dialogs():
    found = []
    buf = ctypes.create_unicode_buffer(64)

    def visit(hwnd, _):
        owner = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(hwnd) and hwnd != h.panel.hwnd:
            user.GetClassNameW(hwnd, buf, 64)
            if buf.value == "#32770":
                found.append(hwnd)
        return True

    user.EnumWindows(ENUM(visit), 0)
    return found


def visible_windows():
    found = []

    def visit(hwnd, _):
        owner = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user.EnumWindows(ENUM(visit), 0)
    return found


def gui_flags():
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    user.GetGUIThreadInfo(tid, ctypes.byref(info))
    # GUI_INMENUMODE 4, GUI_INMOVESIZE 2, GUI_POPUPMENUMODE 16, GUI_SYSTEMMENUMODE 8
    return info.flags & 0x1E, bool(info.hwndCapture)


def mouse_down():
    return bool(
        user.GetAsyncKeyState(0x01) & 0x8000
        or user.GetAsyncKeyState(0x02) & 0x8000
        or user.GetAsyncKeyState(0x04) & 0x8000
    )


def existing_rule():
    return ui.AskLockStatus() == S.Lock or not ui.CanOpenPart()


def combined():
    return (
        existing_rule()
        or not user.IsWindowEnabled(h.main_hwnd)
        and not h.window_disabled
        or gui_flags() != (0, False)
        or mouse_down()
    )


checks = {
    "AskLockStatus == Lock": lambda: ui.AskLockStatus() == S.Lock,
    "not CanOpenPart": lambda: not ui.CanOpenPart(),
    "existing NX_UI_BUSY rule (both)": existing_rule,
    "main window disabled by a modal dialog (IsWindowEnabled)": lambda: (
        not user.IsWindowEnabled(h.main_hwnd)
    ),
    "GetGUIThreadInfo: menu/move-size/capture": gui_flags,
    "mouse button held (GetAsyncKeyState)": mouse_down,
    "EnumWindows: visible #32770 dialogs of NX": visible_dialogs,
    "EnumWindows: all visible NX top-level windows": visible_windows,
    "combined cheap check (rule+enabled+gui+mouse)": combined,
}
cost, now = {}, {}
for name, fn in checks.items():
    xs = []
    for _ in range(N):
        t = pc()
        v = fn()
        xs.append((pc() - t) * 1000)
    cost[name] = summarize(xs)
    now[name] = str(v if not isinstance(v, list) else len(v))
print("JSON:" + json.dumps({"cost": cost, "value_now (agent mode, inside a call)": now}))

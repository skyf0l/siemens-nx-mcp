"""List the visible windows of the NX process to diagnose a blocked bridge.

Development helper, read only: it never clicks or closes anything. When a live
call hangs (dev_reload times out, NX_BRIDGE_UNAVAILABLE), a modal NX dialog is
usually waiting for a person; this shows its title, text and buttons.

    python scripts/dev_nx_windows.py            # NX pid from the bridge descriptor
    python scripts/dev_nx_windows.py --pid 1234
"""

import argparse
import ctypes
import json
import os
import sys
from ctypes import wintypes as w
from pathlib import Path

SKIP_CLASSES = ("Afx:", "NX_SURFACE_WND", "TREELISTCONTROLCLASS", "SysHeader32")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    pid = args.pid
    if pid is None:
        descriptor = Path(os.environ["LOCALAPPDATA"]) / "nx-mcp" / "interactive-bridge.json"
        pid = json.loads(descriptor.read_text(encoding="utf-8"))["pid"]
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [w.HWND]

    def text(hwnd):
        buffer = ctypes.create_unicode_buffer(1024)
        user.GetWindowTextW(hwnd, buffer, 1024)
        return buffer.value

    def cls(hwnd):
        buffer = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    dialogs = 0

    def top(hwnd, _):
        nonlocal dialogs
        owner = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not user.IsWindowVisible(hwnd):
            return True
        is_dialog = cls(hwnd) == "#32770"
        dialogs += is_dialog
        print(f"{'DIALOG' if is_dialog else 'window'}  {text(hwnd)!r}  [{cls(hwnd)}]")

        def child(hwnd_child, _):
            name = cls(hwnd_child)
            if (
                user.IsWindowVisible(hwnd_child)
                and text(hwnd_child)
                and not name.startswith(SKIP_CLASSES)
            ):
                print(f"    {name}: {text(hwnd_child)!r}")
            return True

        user.EnumChildWindows(hwnd, callback(child), 0)
        return True

    user.EnumWindows(callback(top), 0)
    print(f"{dialogs} modal dialog(s) open in NX (pid {pid})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

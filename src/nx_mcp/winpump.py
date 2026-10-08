"""Win32 message pump for journals that stay resident inside NX.

Kept apart from the host so the development loop can keep NX responsive even
when the host code currently on disk fails to import.
"""

from __future__ import annotations

import ctypes


def pump_messages(limit=200):
    """Deliver pending messages for this thread so NX and the panel stay responsive."""
    from ctypes import wintypes as w

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", w.HWND),
            ("message", w.UINT),
            ("wParam", w.WPARAM),
            ("lParam", w.LPARAM),
            ("time", w.DWORD),
            ("pt", w.POINT),
        ]

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.PeekMessageW.argtypes = [ctypes.POINTER(MSG), w.HWND, w.UINT, w.UINT, w.UINT]
    user.PeekMessageW.restype = w.BOOL
    user.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    msg = MSG()
    delivered = 0
    while delivered < limit and user.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
        user.TranslateMessage(ctypes.byref(msg))
        user.DispatchMessageW(ctypes.byref(msg))
        delivered += 1
    return delivered

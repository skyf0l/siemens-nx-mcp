"""Detect NX modal dialogs around a bridge call; never click or close anything.

A modal NX dialog (the NX 2406 "Sheet Metal" switch-to-Modeling warning, say)
blocks NX's UI thread, the call that opened it and every later call until a
person answers it. The tools avoid every dialog they know of; this is the safety
net for the rest. The host scans before a call (an open dialog refuses the call:
``NX_MODAL_DIALOG_OPEN``), watches from a thread during it (NX keeps running
Python threads while a dialog waits) and scans after it; a new dialog is
reported in the result (warning ``NX_MODAL_DIALOG``) and in the activity log.

A blocking dialog is a visible, enabled top-level Win32 dialog (class
``#32770``) of the NX process whose owner is an NX window. NX's own "Work in
Progress" progress box has no owner and is ignored. One scan is an
``EnumWindows`` pass (about 0.4 ms); off Windows it finds nothing.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

DIALOG_CLASS = "#32770"
POLL_SECONDS = 0.25
GW_OWNER = 4


@dataclass(frozen=True)
class Window:
    """What one scan learns about a top-level window (the testable seam)."""

    hwnd: int
    pid: int
    cls: str
    title: str = ""
    visible: bool = True
    enabled: bool = True
    owner: int = 0
    owner_pid: int = 0
    texts: tuple[str, ...] = ()
    buttons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dialog:
    hwnd: int
    title: str
    text: str = ""
    buttons: tuple[str, ...] = field(default=())

    def describe(self) -> str:
        text = " ".join(self.text.split())
        title = self.title or "untitled"
        return f"“{title}”: {text}" if text else f"“{title}”"

    def row(self) -> dict:
        return {"title": self.title, "text": " ".join(self.text.split()),
                "buttons": list(self.buttons)}  # fmt: skip


def blocking(windows: Iterable[Window], pid: int) -> list[Dialog]:
    """The modal dialogs among ``windows`` that block the NX process ``pid``."""
    found = []
    for w in windows:
        if w.pid != pid or w.cls != DIALOG_CLASS or not (w.visible and w.enabled):
            continue
        if not w.owner or w.owner_pid != pid:
            continue  # unowned (NX's "Work in Progress") or another program's
        buttons = tuple(b.replace("&", "") for b in w.buttons if b and b != "X")
        found.append(Dialog(w.hwnd, w.title, "\n".join(t for t in w.texts if t), buttons))
    return found


def new_dialogs(before: Iterable[Dialog], after: Iterable[Dialog]) -> list[Dialog]:
    """Dialogs in ``after`` that were not open in ``before`` (by window handle)."""
    known = {d.hwnd for d in before}
    return [d for d in after if d.hwnd not in known]


def message(dialogs: list[Dialog], *, still_open: bool) -> str:
    shown = "; ".join(d.describe() for d in dialogs)
    if still_open:
        return ("NX shows a modal dialog; NX and every call wait until someone answers it "
                f"in NX. Ask the engineer; the agent never clicks it. {shown}")  # fmt: skip
    return ("NX showed a modal dialog during this call and someone answered it; check "
            f"the result, the answer may have changed what NX did. {shown}")  # fmt: skip


# ---------------------------------------------------------------------------
# Win32


def _windows(pid: int) -> list[Window]:  # pragma: no cover - needs Windows
    import ctypes
    from ctypes import wintypes as w

    user = ctypes.windll.user32
    callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def text(hwnd) -> str:
        buffer = ctypes.create_unicode_buffer(1024)
        user.GetWindowTextW(hwnd, buffer, 1024)
        return buffer.value

    def cls(hwnd) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    def pid_of(hwnd) -> int:
        owner = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        return owner.value

    out: list[Window] = []

    def top(hwnd, _):
        if pid_of(hwnd) != pid or not user.IsWindowVisible(hwnd) or cls(hwnd) != DIALOG_CLASS:
            return True
        owner = user.GetWindow(hwnd, GW_OWNER) or 0
        texts: list[str] = []
        buttons: list[str] = []

        def child(hwnd_child, _):
            name = cls(hwnd_child)
            if name == "Static":
                texts.append(text(hwnd_child))
            elif name == "Button":
                buttons.append(text(hwnd_child))
            return True

        user.EnumChildWindows(hwnd, callback(child), 0)
        out.append(Window(
            int(hwnd), pid, DIALOG_CLASS, text(hwnd), True, bool(user.IsWindowEnabled(hwnd)),
            int(owner), pid_of(owner) if owner else 0, tuple(texts), tuple(buttons),
        ))  # fmt: skip
        return True

    user.EnumWindows(callback(top), 0)
    return out


def scan(pid: int | None = None, windows: Callable[[int], list[Window]] | None = None):
    """The blocking dialogs of process ``pid`` (this process); [] when unknown."""
    if windows is None and sys.platform != "win32":
        return []
    pid = os.getpid() if pid is None else pid
    try:
        return blocking((windows or _windows)(pid), pid)
    except Exception:
        return []  # A diagnostic must never fail the call it watches.


class Watch:
    """Scan from a thread while a call runs; ``on_new`` hears each new dialog once.

    ``stop()`` returns every dialog that appeared during the call (answered or not).
    """

    def __init__(self, before: list[Dialog], *, on_new=None, scanner=scan,
                 interval: float = POLL_SECONDS) -> None:  # fmt: skip
        self.before = list(before)
        self.seen: list[Dialog] = []
        self.on_new = on_new
        self.scanner = scanner
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll(self) -> list[Dialog]:
        fresh = new_dialogs(self.before + self.seen, self.scanner())
        for dialog in fresh:
            self.seen.append(dialog)
            if self.on_new is not None:
                with contextlib.suppress(Exception):
                    self.on_new(dialog)
        return fresh

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.poll()

    def start(self) -> Watch:
        self._thread = threading.Thread(target=self._run, name="nx-mcp-dialogs", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> list[Dialog]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.poll()
        return list(self.seen)

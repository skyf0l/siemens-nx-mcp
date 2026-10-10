"""Detect NX modal dialogs around a bridge call; never click or close anything.

A modal NX dialog (the NX 2406 "Sheet Metal" switch-to-Modeling warning, say)
blocks NX's UI thread, the call that opened it and every later call until a
person answers it. The tools avoid every dialog they know of; this is the safety
net for the rest. The host scans before a call (an open dialog refuses the call:
``NX_MODAL_DIALOG_OPEN``), watches from a thread during it (NX keeps running
Python threads while a dialog waits) and scans after it; a new dialog is
reported in the result (warning ``NX_MODAL_DIALOG``) and in the activity log.

A blocking dialog is a visible, enabled top-level Win32 dialog (class
``#32770``) of the NX process whose owner is an NX window that is disabled: a
modal dialog disables its owner, a modeless box left open does not and is
ignored. When the host disabled the owner itself (it disables NX's main window
while an agent call runs) that proves nothing, so the dialog must also have a modal frame
(``DS_MODALFRAME`` / ``WS_EX_DLGMODALFRAME``, as NX's message boxes have). NX's own
"Work in Progress" progress box has no owner and is ignored.

One scan is an ``EnumWindows`` pass (about 0.4 ms) reading only what needs no
window message (class, owner, enabled and visible flags, styles). Titles and texts
are read with ``InternalGetWindowText`` (no ``WM_GETTEXT`` to NX's busy UI thread)
and only for confirmed modal dialogs. Off Windows it finds nothing.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

DIALOG_CLASS = "#32770"
POLL_SECONDS = 0.25
GW_OWNER = 4
GWL_STYLE, GWL_EXSTYLE = -16, -20
DS_MODALFRAME, WS_EX_DLGMODALFRAME = 0x80, 0x1


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
    owner_enabled: bool = False  # a modal dialog disables its owner
    modal_frame: bool = True  # DS_MODALFRAME or WS_EX_DLGMODALFRAME


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


def is_modal(w: Window, pid: int, host_disabled: Iterable[int] = ()) -> bool:
    """Whether ``w`` is a modal dialog blocking the NX process ``pid``.

    ``host_disabled``: owner windows the host disabled itself (during a call); their
    disabled state proves nothing, so a dialog they own needs a modal frame."""
    if w.pid != pid or w.cls != DIALOG_CLASS or not (w.visible and w.enabled):
        return False
    if not w.owner or w.owner_pid != pid:
        return False  # unowned (NX's "Work in Progress") or another program's
    if w.owner_enabled:
        return False  # modeless: NX still takes input
    return w.owner not in set(host_disabled) or w.modal_frame


def blocking(windows: Iterable[Window], pid: int, host_disabled: Iterable[int] = ()):
    """The modal dialogs among ``windows`` that block the NX process ``pid``."""
    found = []
    disabled = set(host_disabled)
    for w in windows:
        if not is_modal(w, pid, disabled):
            continue
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


def _user32():  # pragma: no cover - needs Windows
    import ctypes
    from ctypes import wintypes as w

    user = ctypes.windll.user32
    user.GetWindowLongW.restype = ctypes.c_long
    user.InternalGetWindowText.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    return ctypes, w, user


def _windows(pid: int) -> list[Window]:  # pragma: no cover - needs Windows
    """The visible top-level ``#32770`` windows of ``pid``; sends no window message."""
    ctypes, w, user = _user32()
    callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

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
        style = user.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        exstyle = user.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        out.append(Window(
            int(hwnd), pid, DIALOG_CLASS, "", True, bool(user.IsWindowEnabled(hwnd)),
            int(owner), pid_of(owner) if owner else 0,
            owner_enabled=bool(owner and user.IsWindowEnabled(owner)),
            modal_frame=bool(style & DS_MODALFRAME or exstyle & WS_EX_DLGMODALFRAME),
        ))  # fmt: skip
        return True

    user.EnumWindows(callback(top), 0)
    return out


def _texts(hwnd: int) -> tuple:  # pragma: no cover - needs Windows
    """``(title, static texts, button texts)`` of a confirmed modal dialog, read with
    ``InternalGetWindowText``: no ``WM_GETTEXT`` is sent to NX's UI thread."""
    ctypes, w, user = _user32()
    callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def text(handle) -> str:
        buffer = ctypes.create_unicode_buffer(1024)
        user.InternalGetWindowText(handle, buffer, 1024)
        return buffer.value

    texts: list[str] = []
    buttons: list[str] = []

    def child(handle, _):
        buffer = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(handle, buffer, 256)
        if buffer.value == "Static":
            texts.append(text(handle))
        elif buffer.value == "Button":
            buttons.append(text(handle))
        return True

    user.EnumChildWindows(hwnd, callback(child), 0)
    return text(hwnd), tuple(texts), tuple(buttons)


def scan(
    pid: int | None = None,
    windows: Callable[[int], list[Window]] | None = None,
    *,
    host_disabled: Iterable[int] = (),
    texts: Callable[[int], tuple] | None = None,
) -> list[Dialog]:
    """The blocking dialogs of process ``pid`` (this process); [] when unknown.

    Texts are read (``texts(hwnd)`` -> ``(title, texts, buttons)``) only for the
    windows :func:`is_modal` confirms; ``host_disabled`` as for :func:`is_modal`."""
    if windows is None and sys.platform != "win32":
        return []
    pid = os.getpid() if pid is None else pid
    disabled = set(host_disabled)
    try:
        modal = [w for w in (windows or _windows)(pid) if is_modal(w, pid, disabled)]
        read = texts if texts is not None else (_texts if windows is None else None)
        if read is not None:
            modal = [_with_texts(w, read) for w in modal]
        return blocking(modal, pid, disabled)
    except Exception:
        return []  # A diagnostic must never fail the call it watches.


def _with_texts(w: Window, read) -> Window:
    title, texts, buttons = read(w.hwnd)
    return replace(w, title=title or w.title, texts=texts or w.texts, buttons=buttons or w.buttons)


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
        self._notify = threading.Lock()
        self._thread: threading.Thread | None = None

    def poll(self, *, notify: bool = True) -> list[Dialog]:
        fresh = new_dialogs(self.before + self.seen, self.scanner())
        for dialog in fresh:
            self.seen.append(dialog)
            if notify and self.on_new is not None:
                with self._notify:  # never after stop(): the call has ended
                    if not self._stop.is_set():
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
        """Every dialog seen, a last scan included; ``on_new`` is not called from now
        on (the call has ended: a late answer must not reach the next call)."""
        with self._notify:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.poll(notify=False)
        return list(self.seen)

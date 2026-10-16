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
(``DS_MODALFRAME`` / ``WS_EX_DLGMODALFRAME``, as the "Sheet Metal" warning has) or be
the active window of NX's UI thread. NX's own "Work in Progress" progress box has no
owner and is ignored; an unowned dialog counts only when NX MCP knows its title
(:data:`KNOWN_DIALOGS`, a license or heartbeat alert) and it has the same proof.

NX's message boxes (live NX 2406: "Edit Sketch: The edit causes curves or dimensions
to be deleted." [OK]) are owned by a *hidden* "Information" ``#32770`` window that
stays enabled, and have no modal frame; the modal loop disables the root owner (NX's
main window) instead. So for a hidden owner the root owner (``GA_ROOTOWNER``) must be
disabled, with the same proof (modal frame or active window) when the host disabled it.
The first version missed exactly this box: NX and every call waited 34 minutes.

One scan is an ``EnumWindows`` pass (about 0.4 ms) reading only what needs no
window message (class, owner, enabled and visible flags, styles). Titles and texts
are read with ``InternalGetWindowText`` (no ``WM_GETTEXT`` to NX's busy UI thread)
and only for confirmed modal dialogs. Off Windows it finds nothing.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

DIALOG_CLASS = "#32770"
POLL_SECONDS = 0.25
# The watchdog: a dialog an agent call opened that nobody answered for ALERT_SECONDS is
# brought to the engineer's attention (the NX MCP panel, NX's taskbar button), again
# every ALERT_REPEAT_SECONDS while it stays open.
ALERT_SECONDS = 20.0
ALERT_REPEAT_SECONDS = 120.0
GW_OWNER = 4
GA_ROOTOWNER = 3
GWL_STYLE, GWL_EXSTYLE = -16, -20
DS_MODALFRAME, WS_EX_DLGMODALFRAME = 0x80, 0x1


class _AnyOwner:
    """``host_disabled`` for a scan from outside NX (the sidecar): any owner may be one
    the host disabled for a call, so a disabled owner alone proves nothing."""

    def __contains__(self, hwnd) -> bool:
        return True

    def __iter__(self):
        return iter(())


ANY_OWNER = _AnyOwner()


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
    owner_visible: bool = True  # NX's message boxes have a hidden owner
    root_owner: int = 0  # GA_ROOTOWNER (NX's main window); 0: the owner itself
    root_enabled: bool = False  # a modal loop disables the root owner
    active: bool = False  # the active window of its (NX's UI) thread


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


def _owners(host_disabled):
    return host_disabled if isinstance(host_disabled, _AnyOwner) else set(host_disabled)


def is_modal(w: Window, pid: int, host_disabled: Iterable[int] = ()) -> bool:
    """Whether ``w`` is a modal dialog blocking the NX process ``pid``.

    ``host_disabled``: owner windows the host disabled itself (during a call); their
    disabled state proves nothing, so a dialog they own needs a modal frame or must be
    the active window of NX's UI thread (``ANY_OWNER``: every owner, from outside NX)."""
    if w.pid != pid or w.cls != DIALOG_CLASS or not (w.visible and w.enabled):
        return False
    if not w.owner or w.owner_pid != pid:
        return False  # unowned (NX's "Work in Progress") or another program's
    disabled = _owners(host_disabled)
    proof = w.modal_frame or w.active
    if not w.owner_visible:  # an NX message box: its hidden owner stays enabled
        if w.root_enabled:
            return False  # NX still takes input: not modal
        return (w.root_owner or w.owner) not in disabled or proof
    if w.owner_enabled:
        return False  # modeless: NX still takes input
    return w.owner not in disabled or proof


def blocking(windows: Iterable[Window], pid: int, host_disabled: Iterable[int] = ()):
    """The modal dialogs among ``windows`` that block the NX process ``pid``."""
    found = []
    disabled = _owners(host_disabled)
    for w in windows:
        if not is_modal(w, pid, disabled):
            continue
        buttons = tuple(b.replace("&", "") for b in w.buttons if b and b != "X")
        found.append(Dialog(w.hwnd, w.title, "\n".join(t for t in w.texts if t), buttons))
    return found


# Modal dialogs NX MCP knows by title: what each one is and what answers it. The tools
# avoid them (a large sketch's shaded-region prompt is turned off at host start and
# before sketch work; update failures undo themselves during a call), so one that still
# shows is named with its own hint. Matched on the start of the title, any case.
KNOWN_DIALOGS = (
    (
        "Large Sketch Performance",
        "NX asks whether to keep shading the regions of a sketch with many curves",
        "Ask the engineer to answer it in NX (turning region shading off keeps the "
        "sketch); add the curves in calls of at most 50 each",
    ),
    (
        "Error During Update",
        "an NX model update failed (for example an object that depends on itself)",
        "Ask the engineer to choose Undo in the NX dialog, then fix the input NX names "
        "(a body or feature later in the timeline than the object that uses it)",
    ),
    (
        "Sheet Metal",
        "NX asks before leaving the Sheet Metal application",
        "Ask the engineer to answer it in NX; then call nx_activate_application",
    ),
    (
        "Heartbeat Failure",
        "NX lost its connection to the license server",
        "Ask the engineer to check the network to the license server, close the dialog "
        "and restart NX if it comes back",
    ),
    (
        "License Not Available",
        "NX found no license for the command",
        "Ask the engineer to close the dialog in NX; the tool needs a license this seat "
        "does not have",
    ),
)


# Known dialogs no engineer command opens (NX's own alerts): a call waiting for a busy
# engineer is answered at once while one is open, instead of after the busy wait.
ALERTS = ("Heartbeat Failure",)


def is_alert(title: str | None) -> bool:
    """Whether ``title`` names one of :data:`ALERTS` (start of the title, any case)."""
    text = " ".join(str(title or "").split()).casefold()
    return any(text.startswith(name.casefold()) for name in ALERTS)


def known(title: str | None) -> tuple[str, str] | None:
    """``(what it is, hint)`` of a dialog NX MCP knows by its title, else None."""
    text = " ".join(str(title or "").split()).casefold()
    for name, what, hint in KNOWN_DIALOGS:
        if text.startswith(name.casefold()):
            return what, hint
    return None


def hint_for(title: str | None, default: str) -> str:
    """The hint for a blocking dialog: its own when known (:data:`KNOWN_DIALOGS`), else
    ``default``."""
    found = known(title)
    return found[1] if found else default


def new_dialogs(before: Iterable[Dialog], after: Iterable[Dialog]) -> list[Dialog]:
    """Dialogs in ``after`` that were not open in ``before`` (by window handle)."""
    known = {d.hwnd for d in before}
    return [d for d in after if d.hwnd not in known]


def blocked_message(row: dict, *, outcome: str) -> str:
    """The error text for a call that met NX blocked by the modal dialog ``row``
    (``{title, text, tool, part, since}``): what blocks NX, since when, by which call."""
    title = row.get("title") or "untitled"
    text = " ".join(str(row.get("text") or "").split())
    shown = f"“{title}”" + (f": {text}" if text else "")
    what = known(row.get("title"))
    if what is not None and not text:
        shown += f" ({what[0]})"
    since = row.get("since")
    age = f" {max(0, int(time.time() - since))} s ago" if isinstance(since, (int, float)) else ""
    where = f" in {row['part']}" if row.get("part") else ""
    context = f" (opened{age} during {row['tool']}{where})" if row.get("tool") else ""
    tail = (
        "The outcome of this call is unknown until then."
        if outcome == "unknown"
        else "This call did not run."
    )
    return (
        f"NX is blocked by its modal dialog {shown}{context}. "
        "NX and every call wait until someone answers it in NX; the agent never clicks it. " + tail
    )


def message(dialogs: list[Dialog], *, still_open: bool) -> str:
    shown = "; ".join(d.describe() for d in dialogs)
    if still_open:
        hints = [found[1] for found in (known(d.title) for d in dialogs) if found]
        tail = f" {hints[0]}." if hints else ""
        return ("NX shows a modal dialog; NX and every call wait until someone answers it "
                f"in NX. Ask the engineer; the agent never clicks it. {shown}{tail}")  # fmt: skip
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
    _, w, user = _user32()
    callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    class GuiThreadInfo(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD), ("hwndActive", w.HWND),
                    ("hwndFocus", w.HWND), ("hwndCapture", w.HWND), ("hwndMenuOwner", w.HWND),
                    ("hwndMoveSize", w.HWND), ("hwndCaret", w.HWND), ("rcCaret", w.RECT)]  # fmt: skip

    def active(hwnd) -> bool:
        """``hwnd`` is the active window of its thread (GetGUIThreadInfo sends no message)."""
        info = GuiThreadInfo(cbSize=ctypes.sizeof(GuiThreadInfo))
        thread = user.GetWindowThreadProcessId(hwnd, None)
        if not user.GetGUIThreadInfo(thread, ctypes.byref(info)):
            return False
        return int(info.hwndActive or 0) == int(hwnd)

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
        root = (user.GetAncestor(hwnd, GA_ROOTOWNER) or 0) if owner else 0
        style = user.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        exstyle = user.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        out.append(Window(
            int(hwnd), pid, DIALOG_CLASS, "", True, bool(user.IsWindowEnabled(hwnd)),
            int(owner), pid_of(owner) if owner else 0,
            owner_enabled=bool(owner and user.IsWindowEnabled(owner)),
            modal_frame=bool(style & DS_MODALFRAME or exstyle & WS_EX_DLGMODALFRAME),
            owner_visible=bool(owner and user.IsWindowVisible(owner)),
            root_owner=int(root), root_enabled=bool(root and user.IsWindowEnabled(root)),
            active=active(hwnd),
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
    disabled = _owners(host_disabled)
    try:
        listed = (windows or _windows)(pid)
        modal = [w for w in listed if is_modal(w, pid, disabled)]
        loose = [w for w in listed if _unowned(w, pid)]
        read = texts if texts is not None else (_texts if windows is None else None)
        if read is not None:
            modal = [_with_texts(w, read) for w in modal]
            loose = [_with_texts(w, read) for w in loose]
        return blocking(modal, pid, disabled) + _known_unowned(loose)
    except Exception:
        return []  # A diagnostic must never fail the call it watches.


def _unowned(w: Window, pid: int) -> bool:
    """A visible, enabled, unowned ``#32770`` window of NX: not modal by the owner rule
    (NX's "Work in Progress" box), but it may be an alert NX MCP knows by title."""
    return w.pid == pid and w.cls == DIALOG_CLASS and w.visible and w.enabled and not w.owner


def _known_unowned(windows: Iterable[Window]) -> list[Dialog]:
    """Unowned dialogs whose title NX MCP knows (:data:`KNOWN_DIALOGS`: a license or
    heartbeat alert may have no owner window) with the proof asked of a dialog whose
    owner the host disabled: a modal frame, or the active window of NX's UI thread."""
    found = []
    for w in windows:
        if known(w.title) is None or not (w.modal_frame or w.active):
            continue
        buttons = tuple(b.replace("&", "") for b in w.buttons if b and b != "X")
        found.append(Dialog(w.hwnd, w.title, "\n".join(t for t in w.texts if t), buttons))
    return found


def _with_texts(w: Window, read) -> Window:
    title, texts, buttons = read(w.hwnd)
    return replace(w, title=title or w.title, texts=texts or w.texts, buttons=buttons or w.buttons)


class Watch:
    """Scan from a thread while a call runs; ``on_new`` hears each new dialog once and
    ``on_gone`` each reported dialog that closed again (someone answered it).

    The watchdog: ``on_stuck(dialog, seconds)`` hears a new dialog still open
    ``alert_after`` seconds after it appeared, then every ``alert_repeat`` seconds while
    it stays open (the host alerts the engineer). ``clock``: monotonic seconds (tests).

    ``stop()`` returns every dialog that appeared during the call (answered or not).
    """

    def __init__(self, before: list[Dialog], *, on_new=None, on_gone=None, on_stuck=None,
                 scanner=scan, interval: float = POLL_SECONDS,
                 alert_after: float = ALERT_SECONDS, alert_repeat: float = ALERT_REPEAT_SECONDS,
                 clock=time.monotonic) -> None:  # fmt: skip
        self.before = list(before)
        self.seen: list[Dialog] = []
        self.gone: list[Dialog] = []
        self.on_new = on_new
        self.on_gone = on_gone
        self.on_stuck = on_stuck
        self.scanner = scanner
        self.interval = interval
        self.alert_after = alert_after
        self.alert_repeat = alert_repeat
        self.clock = clock
        self._since: dict[int, float] = {}  # hwnd -> when the dialog appeared
        self._alerted: dict[int, float] = {}  # hwnd -> when the engineer was last alerted
        self._stop = threading.Event()
        self._notify = threading.Lock()
        self._thread: threading.Thread | None = None

    def poll(self, *, notify: bool = True) -> list[Dialog]:
        now = self.scanner()
        moment = self.clock()
        fresh = new_dialogs(self.before + self.seen, now)
        for dialog in fresh:
            self.seen.append(dialog)
            self._since[dialog.hwnd] = moment
            self._tell(self.on_new, dialog, notify)
        open_now = {d.hwnd for d in now}
        for dialog in [d for d in self.seen if d.hwnd not in open_now and d not in self.gone]:
            self.gone.append(dialog)
            self._tell(self.on_gone, dialog, notify)
        for dialog in [d for d in self.seen if d.hwnd in open_now and d not in self.gone]:
            self._watchdog(dialog, moment, notify)
        return fresh

    def _watchdog(self, dialog: Dialog, moment: float, notify: bool) -> None:
        """Alert once the dialog has waited ``alert_after`` s, then every ``alert_repeat``."""
        since = self._since.get(dialog.hwnd, moment)
        last = self._alerted.get(dialog.hwnd)
        due = since + self.alert_after if last is None else last + self.alert_repeat
        if self.on_stuck is None or moment < due:
            return
        self._alerted[dialog.hwnd] = moment
        seconds = moment - since
        self._tell(lambda d: self.on_stuck(d, seconds), dialog, notify)

    def _tell(self, listener, dialog: Dialog, notify: bool) -> None:
        if notify and listener is not None:
            with self._notify:  # never after stop(): the call has ended
                if not self._stop.is_set():
                    with contextlib.suppress(Exception):
                        listener(dialog)

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


def process_alive(pid: int | None) -> bool | None:
    """Whether process ``pid`` runs (None: unknown, off Windows). Never signals it."""
    if not pid or sys.platform != "win32":
        return None
    try:  # pragma: no cover - needs Windows
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        return None

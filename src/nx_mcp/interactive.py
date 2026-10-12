"""Windows NX UI-thread host. No background thread calls NXOpen.

Timer mode (NX 2606): the journal returns after installing a Win32 timer. NX's
existing message pump invokes TIMERPROC on the registering thread; the pinned
callback drains one call.

Resident mode (NX 2406): the NX startup application
(``nx_app/startup/nx_mcp_startup.py``) stays loaded for the session and ticks the
host from its own UI-thread timer, so lazy imports and NXOpen namespaces resolve
where they were set up.

Every bridge call runs through ``host.dispatch`` and the catalogue kernel with the
host's ``NXServices`` (``self.services``).

Panel modes (the two radio buttons on the NX MCP panel; only the engineer changes
them):

- ``auto`` ("Auto", the default): the engineer works in NX. Each agent call takes
  NX for itself only (NX's main window is disabled, no ``UI.LockAccess``), runs
  and gives NX back. Reads never wait; other calls wait silently while the
  engineer is busy in an NX command or dialog (``engineer_busy``), at most
  BUSY_WAIT_SECONDS, and a non-read call is refused while a modal NX dialog is
  open (``NX_MODAL_DIALOG_OPEN``). No request card: long steps simply run.
- ``ask`` ("Ask me first", alias ``manual``): every call but the status read waits
  for the engineer's answer on the request card (Run once, Run & switch to Auto,
  Not now). The card shows one call at a time, the oldest waiting one, and an answer
  applies to that call only; each call has its own HOLD_SECONDS clock from the moment
  the card shows it. "Run & switch to Auto" runs the shown call and switches the mode:
  the calls still queued then run under Auto's rules. A call that needs no answer (the
  status read, an approved call) never waits behind the card.

While two or more parts are loaded, the running line, the card and every activity
line name the target part's file first (``[cover.prt] Extrude ...``); with one part
the lines are as they were. A development snippet (``nx_dev_python``, also the acceptance
runner's engineer step) has no target part: its lines name none.

"Parts in use" (below the activity list): one line per agent part (created, opened or
granted; never the engineer's own): file, shown (the active NX tab), tab or background,
modified / saved / new, and the last agent step in it with its time. The list scrolls;
"Show part" (or a double click) activates that part's NX tab (``kit.parts.activate``,
undo marks kept): an engineer action, never an agent's. The list refreshes after each
call (``publish``).

An engineer change (``_check_engineer_change``) is a change of NX's undo marks or of the
loaded parts between two agent calls; a display, work part or tab change alone (the
engineer clicking a tab, "Show part") is not an edit and leaves the agents' undo
history alone. The ENGINEER log line says what changed. A queued call whose client gave up (closed its socket, or
its ``expires_at`` passed) is answered NX_CALL_EXPIRED and never shown or run; the
activity log notes it (``Skipped ...: the agent stopped waiting``).

Requests wake the host at once: the listening socket posts FD_ACCEPT to the
panel window (``WSAAsyncSelect``), which runs a tick right away. NX holds
Python's GIL while it is idle, so a tick also gives the bridge worker threads
the GIL until the request is queued and the answer written (_let_bridge_run).
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import threading
import time
import uuid
from pathlib import Path

from nx_mcp.core.errors import NXError

_host = None
_retired = []

# Ask me first: agent requests wait in the queue for an answer from the panel.
HOLD_SECONDS = 60
# Auto: a step waits at most this long while the engineer is busy in NX. The
# bridge (QUEUE_TIMEOUT) waits longer, so this answer always arrives first.
BUSY_WAIT_SECONDS = 120
PAD_RIGHT = 16  # Panel margin right of the text and Stop bridge, in pixels.
MIN_WINDOW = (560, 300)  # Opening and smallest panel window size.
MIN_CLIENT = (520, 200)  # Layout floor while Windows shrinks the client area.
PANEL_ROWS = 8  # Panel body lines at the opening size; resizing changes host.panel_rows.
NOTICE_SECONDS = 30  # A notice replaces the idle line this long.

MODES = ("auto", "ask")
MODE_LABELS = {"auto": "Auto", "ask": "Ask me first"}
MODE_ALIASES = {"manual": "ask"}  # the old name of Ask me first
STATUS_TOOL = "nx_get_status"  # the status read (domains/session/status.py)
DEV_PYTHON = "nx_dev_python"  # a development snippet: no target part

# Panel window messages: the bridge's listening socket has a connection
# (WSAAsyncSelect), and "tick again now" (posted by the host itself).
WM_BRIDGE_ACCEPT = 0x8000 + 0x4E1
WM_HOST_WAKE = 0x8000 + 0x4E2
FD_ACCEPT = 0x08
# Giving the bridge worker threads the GIL: stop when no worker made progress
# for one slice (it waits on its socket, not on us), and after the budget.
BRIDGE_SLICE = 0.01
ARRIVAL_BUDGET = 0.05
REPLY_BUDGET = 0.1


def _read_only(method):
    """Catalogue tools whose effect is ``read``."""
    from nx_mcp.host.dispatch import is_read_only

    return is_read_only(method)


def _model_unchanged(method):
    """Read and view steps: grouped in the panel history (model unchanged)."""
    from nx_mcp.host.dispatch import effect_of

    return effect_of(method) in ("read", "view")


def _display_changed(before, now):
    """Whether two ``model_fingerprint`` values name different display parts (the
    engineer switched the display, which drops NX's undo marks)."""
    try:
        return before[4] != now[4]
    except (IndexError, TypeError):
        return False


FINGERPRINT_PARTS = ("NX's newest undo mark", "the number of NX undo marks", "the loaded parts")


def _edit_changes(before, now):
    """What an engineer edit changed between two ``model_fingerprint`` values: NX's undo
    marks or the loaded parts (``FINGERPRINT_PARTS``). A display, work part or tab change
    alone is no edit (empty list). An unknown fingerprint counts as a change."""
    if before is None or now is None:
        return ["an unknown NX state"]
    if not isinstance(before, tuple) or not isinstance(now, tuple) or len(before) != len(now):
        return [] if before == now else ["the model"]
    if len(now) < 5:
        return [] if before == now else ["the model"]
    return [name for i, name in enumerate(FINGERPRINT_PARTS) if before[i] != now[i]]


def _envelope(error):
    """The catalogue envelope a failed call carries (``details.envelope``), or None."""
    details = getattr(error, "details", None)
    envelope = details.get("envelope") if isinstance(details, dict) else None
    return envelope if isinstance(envelope, dict) else None


def _show_blocked(envelope, blocked):
    """Name the modal dialog that blocks NX in a status envelope (``control`` and a
    warning); the dialog's window handle stays inside the host."""
    from nx_mcp.host import dialogs

    row = {k: v for k, v in blocked.items() if k != "hwnd"}
    since = row.get("since")
    row["seconds"] = round(time.time() - since) if isinstance(since, (int, float)) else None
    control = (envelope.get("data") or {}).get("control")
    if isinstance(control, dict):
        control["blocked_by_dialog"] = row
        control["activity"] = "blocked_by_dialog"
    message = dialogs.blocked_message(blocked, outcome="not_started").rsplit(" This call", 1)[0]
    envelope.setdefault("warnings", []).append({"code": "NX_MODAL_DIALOG", "message": message})


def _running_tail(method):
    """Catalogue tools of cost light do not pause NX noticeably (arch. 3.3)."""
    from nx_mcp.host.dispatch import cost_of

    return " — running" if cost_of(method) == "light" else " — running, NX may pause"


def _span(seconds):
    return f"{seconds // 60} minutes" if seconds >= 120 else f"{seconds} s"


def remembered_mode(state_dir):
    """The panel mode last chosen in this NX process (kept across dev reloads)."""
    try:
        data = json.loads((Path(state_dir) / "ui-mode.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if isinstance(data, dict) and data.get("pid") == os.getpid() and data.get("mode") in MODES:
        return data["mode"]
    return None


# NXOpen namespaces are submodules: NXOpen.Features is an attribute only after
# "import NXOpen.Features". A resident journal's interpreter starts with none.
NXOPEN_NAMESPACES = (
    "NXOpen",
    "NXOpen.UF",
    "NXOpen.Features",
    "NXOpen.Features.SheetMetal",
    "NXOpen.GeometricUtilities",
    "NXOpen.GeometricAnalysis",
    "NXOpen.Annotations",
    "NXOpen.Assemblies",
    "NXOpen.Display",
    "NXOpen.Drawings",
    "NXOpen.Gateway",
    "NXOpen.Positioning",
    "NXOpen.Preferences",
)


def import_nxopen_namespaces(names=NXOPEN_NAMESPACES):
    """Import NXOpen namespaces into this interpreter; return the ones that failed."""
    import importlib

    missing = []
    for name in names:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            missing.append(f"{name} ({exc})")
    return missing


def nx_main_window():
    """Find the main window owned by this NX process, before creating our panel."""
    from ctypes import wintypes as w

    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
    user.IsWindowVisible.argtypes = [w.HWND]
    windows = []

    def visit(hwnd, param):
        pid = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid() and user.IsWindowVisible(hwnd):
            rect = w.RECT()
            user.GetWindowRect(hwnd, ctypes.byref(rect))
            windows.append(((rect.right - rect.left) * (rect.bottom - rect.top), hwnd))
        return True

    callback = callback_type(visit)
    user.EnumWindows(callback, 0)
    if not windows:
        raise RuntimeError("NX has no visible main window")
    return max(windows)[1]


# Panel control IDs.
ID_STOP, ID_DECLINE, ID_OPEN_LOG, ID_GRANT, ID_RUN_ONCE, ID_RUN_AUTO = 103, 104, 105, 106, 107, 108
ID_SHOW_PART, ID_PARTS = 109, 110
LBN_DBLCLK = 2  # a double click on a "Parts in use" line
PARTS_ROWS = (2, 6)  # "Parts in use" list height in lines: at least, at most
ID_MODES = {111: "auto", 113: "ask"}
ANSWERS = {ID_RUN_ONCE: "run_once", ID_RUN_AUTO: "run_auto", ID_DECLINE: "decline"}


class ControlPanel:
    def __init__(self, host):
        from ctypes import wintypes as w

        self.host = host
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", w.UINT),
                ("lpfnWndProc", self.proc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HINSTANCE),
                ("hIcon", w.HICON),
                ("hCursor", w.HANDLE),
                ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR),
                ("lpszClassName", w.LPCWSTR),
            ]

        self.user.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        self.user.DefWindowProcW.restype = ctypes.c_ssize_t
        self.user.CreateWindowExW.argtypes = [
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
        self.user.CreateWindowExW.restype = w.HWND
        self.user.SetWindowTextW.argtypes = [w.HWND, w.LPCWSTR]
        self.user.EnableWindow.argtypes = [w.HWND, ctypes.c_int]
        self.user.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
        self.user.MoveWindow.argtypes = [
            w.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            w.BOOL,
        ]
        self.user.UpdateWindow.argtypes = [w.HWND]
        self.user.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        self.user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        self.user.DestroyWindow.argtypes = [w.HWND]
        self.user.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
        self.user.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
        self.user.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
        self.proc = self.proc_type(self._message)
        cls = WNDCLASS()
        cls.lpfnWndProc = self.proc
        cls.hbrBackground = 6
        cls.lpszClassName = "NxMcpPanel" + uuid.uuid4().hex
        self._class = cls
        if not self.user.RegisterClassW(ctypes.byref(cls)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Resizable (WS_THICKFRAME), opening at its minimum size: the activity list
        # grows with the window, the buttons stay at the bottom and Stop bridge at
        # the right, PAD_RIGHT inside the edge (_layout places every control).
        self.hwnd = self.user.CreateWindowExW(
            0x80,
            cls.lpszClassName,
            "NX MCP control",
            0x10CC0000,
            40,
            60,
            MIN_WINDOW[0],
            MIN_WINDOW[1],
            self.host.main_hwnd,
            None,
            None,
            None,
        )
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        def control(cls_name, text, style, ident=None):
            return self.user.CreateWindowExW(
                0, cls_name, text, style, 0, 0, 0, 0, self.hwnd, ident, None, None
            )

        # Mode: (•) Auto ( ) Ask me first. BS_RADIOBUTTON (not auto): a click
        # only asks for the mode; the host sets the check marks.
        self.mode_label = control("STATIC", "Mode:", 0x50000000)
        self.radios = {
            "auto": control("BUTTON", "Auto", 0x50010004, 111),
            "ask": control("BUTTON", "Ask me first", 0x50000004, 113),
        }
        # One list, newest first: the running step (or idle line / request card) on
        # top, then recent activity. The title says the mode and what the agent does.
        # SS_LEFTNOWORDWRAP (0xC): an entry never wraps into the next one; a line too
        # long for a high-DPI font is clipped at the right edge instead.
        self.label = control("STATIC", "Starting interactive bridge...", 0x5000000C)
        # Production only: the engineer allows the agent to change the work part
        # (a part the agent neither created nor opened). BS_CHECKBOX, hidden until
        # needed; the host owns the checked state (show_grant).
        self.grant = control("BUTTON", "", 0x40010002, ID_GRANT)
        # "Parts in use": the agent's parts, one line each; LBS_NOTIFY (double click),
        # WS_VSCROLL (a long list scrolls), LBS_NOINTEGRALHEIGHT (exact layout height).
        self.parts_label = control("STATIC", "Parts in use", 0x50000000)
        self.parts = control("LISTBOX", "", 0x50A00101, ID_PARTS)
        self.show_part = control("BUTTON", "Show part", 0x50010000, ID_SHOW_PART)
        self._parts_rows: list = []
        # The request card's answers replace "Open log" while the agent waits for
        # one. Stop sits apart so it is not hit by mistake.
        self.run_once = control("BUTTON", "Run once", 0x40010000, ID_RUN_ONCE)
        self.run_auto = control("BUTTON", "Run && switch to Auto", 0x40010000, ID_RUN_AUTO)
        self.decline = control("BUTTON", "Not now", 0x40010000, ID_DECLINE)
        self.open_log = control("BUTTON", "Open log", 0x50010000, ID_OPEN_LOG)
        self.stop = control("BUTTON", "Stop bridge", 0x50010000, ID_STOP)
        # The dialog font fits a full activity line; the default system font does not.
        self.line_height = 16
        try:
            gdi = ctypes.WinDLL("gdi32")
            gdi.GetStockObject.restype = ctypes.c_void_p
            font = gdi.GetStockObject(17)  # DEFAULT_GUI_FONT
            children = (
                self.mode_label,
                *self.radios.values(),
                self.label,
                self.grant,
                self.parts_label,
                self.parts,
                self.show_part,
                self.run_once,
                self.run_auto,
                self.decline,
                self.open_log,
                self.stop,
            )
            for child in children:
                self.user.SendMessageW(child, 0x30, font, 1)  # WM_SETFONT
            self.line_height = self._text_height(gdi, font) or 16
        except Exception as exc:
            self.host.last_error = "Panel font: " + str(exc)
        rect = w.RECT()
        if self.user.GetClientRect(self.hwnd, ctypes.byref(rect)):
            self._layout(rect.right, rect.bottom)

    def _text_height(self, gdi, font):
        """Pixel height of one activity line in the panel font (0 if unknown)."""
        self.user.GetDC.restype = ctypes.c_void_p
        self.user.GetDC.argtypes = [ctypes.c_void_p]
        self.user.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi.GetTextMetricsW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        dc = self.user.GetDC(self.label)
        if not dc:
            return 0
        try:
            gdi.SelectObject(dc, font)
            metrics = (ctypes.c_long * 15)()  # TEXTMETRICW starts with tmHeight
            return metrics[0] if gdi.GetTextMetricsW(dc, metrics) else 0
        finally:
            self.user.ReleaseDC(self.label, dc)

    def _layout(self, width, height):
        """Place the controls in a width x height client area (WM_SIZE)."""
        if not hasattr(self, "stop"):
            return  # WM_SIZE while CreateWindowEx builds the window
        self._client = (width, height)
        move = self.user.MoveWindow
        right = max(width - PAD_RIGHT, MIN_CLIENT[0] - PAD_RIGHT)
        buttons = max(height - 12 - 28, MIN_CLIENT[1] - 12 - 28)
        move(self.mode_label, 12, 12, 48, 20, True)
        move(self.radios["auto"], 66, 8, 72, 24, True)
        move(self.radios["ask"], 144, 8, 124, 24, True)
        # The grant checkbox row is reserved only while it shows (show_grant).
        grant_row = 28 if getattr(self, "_grant", None) is not None else 0
        area = buttons - grant_row - 8 - 38
        line = max(self.line_height, 1)
        low, high = PARTS_ROWS
        rows = max(low, min(high, len(getattr(self, "_parts_rows", ()) or ()) or low))
        parts_height = min(rows * line + 6, max(low * line + 6, area // 3))
        list_height = max(line * 3, area - parts_height - 22)
        move(self.label, 12, 38, right - 12, list_height, True)
        top = 38 + list_height + 4
        move(self.parts_label, 12, top, right - 12, 18, True)
        move(self.parts, 12, top + 18, right - 12, parts_height, True)
        move(self.grant, 12, buttons - 28, right - 12, 22, True)
        move(self.run_once, 12, buttons, 110, 28, True)
        move(self.run_auto, 128, buttons, 170, 28, True)
        move(self.decline, 304, buttons, 110, 28, True)
        move(self.open_log, 12, buttons, 110, 28, True)
        move(self.show_part, 128, buttons, 110, 28, True)
        move(self.stop, right - 130, buttons, 130, 28, True)
        self.host.panel_rows = max(3, min(40, list_height // line))

    def _message(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_BRIDGE_ACCEPT:
                self.host.bridge_ready()
                return 0
            if msg == WM_HOST_WAKE:
                self.host.tick()
                return 0
            if msg == 0x111:
                ident = int(wp) & 0xFFFF
                if ident in ID_MODES:
                    self.host.requested_mode = ID_MODES[ident]
                elif ident in ANSWERS:
                    self.host.answer_requested = ANSWERS[ident]
                elif ident == ID_OPEN_LOG:
                    self.host.open_log_requested = True
                elif ident == ID_GRANT:
                    self.host.grant_toggle_requested = True
                elif ident == ID_SHOW_PART or (
                    ident == ID_PARTS and (int(wp) >> 16) & 0xFFFF == LBN_DBLCLK
                ):
                    key = self.selected_part()
                    if key is not None:
                        self.host.show_part_requested = key
                elif ident == ID_STOP:
                    self.host.stop_requested = True
                return 0
            if msg == 0x0005:  # WM_SIZE: client width and height in lp
                self._layout(lp & 0xFFFF, (lp >> 16) & 0xFFFF)
                return 0
            if msg == 0x0024:  # WM_GETMINMAXINFO: ptMinTrackSize is longs 6 and 7
                info = ctypes.cast(lp, ctypes.POINTER(ctypes.c_long * 10)).contents
                info[6], info[7] = MIN_WINDOW
                return 0
            if msg == 0x10:
                self.host.stop_requested = True
                return 0
        except BaseException as exc:
            self.host.last_error = str(exc)
        return self.user.DefWindowProcW(hwnd, msg, wp, lp)

    def update(self, text, title=None, mode=None, card=None):
        """Body text, window title, the checked mode, and the request card's buttons:
        None (no card) or "ask" (Run once, Run & switch to Auto, Not now)."""
        state = (text, title, mode, card)
        if state == getattr(self, "_text", None):
            return
        previous = getattr(self, "_text", None) or (None, None, None, "unset")
        self._text = state
        self.user.SetWindowTextW(self.label, text)
        if title is not None:
            self.user.SetWindowTextW(self.hwnd, title)
        if mode is not None and mode != previous[2]:
            for name, radio in self.radios.items():
                self.user.SendMessageW(radio, 0xF1, 1 if name == mode else 0, 0)  # BM_SETCHECK
        if card != previous[3]:
            self.user.ShowWindow(self.run_once, 5 if card else 0)
            self.user.ShowWindow(self.run_auto, 5 if card else 0)
            self.user.ShowWindow(self.decline, 5 if card else 0)
            self.user.ShowWindow(self.open_log, 0 if card else 5)
            self.user.ShowWindow(self.show_part, 0 if card else 5)
        # Paint only this panel before native work. Never pump arbitrary NX
        # messages here: that would permit reentrant modeling calls.
        self.user.UpdateWindow(self.label)
        self.user.UpdateWindow(self.hwnd)

    def show_grant(self, grant):
        """Show the "Allow agent to change <part>" checkbox, or hide it (None)."""
        if grant == getattr(self, "_grant", None):
            return
        shown_before = getattr(self, "_grant", None) is not None
        self._grant = grant
        if (grant is not None) != shown_before and hasattr(self, "_client"):
            self._layout(*self._client)  # the list gives or takes the checkbox row
        if grant is None:
            self.user.ShowWindow(self.grant, 0)
            return
        self.user.SetWindowTextW(self.grant, f"Allow agent to change {grant['name'][:60]}")
        self.user.SendMessageW(self.grant, 0xF1, 1 if grant["granted"] else 0, 0)  # BM_SETCHECK
        self.user.ShowWindow(self.grant, 5)

    def show_parts(self, rows):
        """Fill "Parts in use" with ``[(part key, line)]``, keeping the selected part."""
        rows = list(rows)
        if rows == self._parts_rows:
            return
        selected = self.selected_part()
        resize = (len(rows) != len(self._parts_rows)) and hasattr(self, "_client")
        self._parts_rows = rows
        send = self.user.SendMessageW
        send(self.parts, 0x0184, 0, 0)  # LB_RESETCONTENT
        for _key, text in rows or [(None, "No agent part is loaded")]:
            buffer = ctypes.create_unicode_buffer(text)
            send(self.parts, 0x0180, 0, ctypes.cast(buffer, ctypes.c_void_p).value)  # LB_ADDSTRING
        index = next((i for i, (key, _t) in enumerate(rows) if key == selected), -1)
        if index >= 0:
            send(self.parts, 0x0186, index, 0)  # LB_SETCURSEL
        if resize:
            self._layout(*self._client)

    def selected_part(self):
        """The part key of the selected "Parts in use" line, or None."""
        index = int(self.user.SendMessageW(self.parts, 0x0188, 0, 0))  # LB_GETCURSEL
        rows = self._parts_rows
        return rows[index][0] if 0 <= index < len(rows) else None

    def wake(self):
        """Ask for another tick as soon as NX dispatches messages again."""
        self.user.PostMessageW(self.hwnd, WM_HOST_WAKE, 0, 0)

    def close(self):
        self.user.DestroyWindow(self.hwnd)
        self.user.UnregisterClassW(self._class.lpszClassName, None)


class InteractiveHost:
    dev = False
    driver = "timer"  # timer (2606 journal), journal (2406 resident), startup (NX startup app)
    resident = False
    timer = None
    mode = "auto"
    requested_mode = None
    panel_rows = PANEL_ROWS
    answer_requested = None
    # The request card: calls waiting for the engineer's answer.
    card = ()
    waiting_since = None
    waiting_count = 0
    waiting_summary = None
    waiting_intent = None
    # Auto: the call waiting until the engineer finishes an NX command.
    busy_wait = None
    simulated_busy = False  # Development seam: makes engineer_busy() true.
    notice = None
    notice_until = None
    open_log_requested = False
    grant_toggle_requested = False
    show_part_requested = None  # the part key whose tab the engineer asked to show
    _parts_lines = ()  # "Parts in use": [(part key, line)] as last published
    current_summary = None
    current_part = None  # the running call's target file (two or more parts loaded)
    current_intent = None
    current_started_at = None
    window_disabled = False
    _activity = None
    _light = False  # A call runs with NX taken for that call only (Auto, approved).
    _engineer_had_nx = True  # The engineer could edit since the agent's last call.
    _has_fingerprint = False
    _fingerprint = None
    _fast_accept = False
    _rewake = False
    _uf = None
    # The modal NX dialog blocking the running call (``_dialog_appeared``): {title, text,
    # buttons, hwnd, tool, part, since}; None while NX is not blocked.
    blocked_by = None

    def __init__(self, workspace, descriptor_path, resident=False):
        import secrets

        import NXOpen

        from nx_mcp import nx_bridge
        from nx_mcp.bridge import (
            QUEUE_TIMEOUT,
            BridgeDescriptor,
            BridgeServer,
            MainThreadDispatcher,
            run_timeout,
        )
        from nx_mcp.host.services import NXServices
        from nx_mcp.workspace import Workspace, default_workspace

        self.session = NXOpen.Session.GetSession()
        self.nx = NXOpen
        self.ui = NXOpen.UI.GetUI()
        if self.session.IsBatch:
            raise RuntimeError("Interactive host requires a graphical NX session")
        self.thread = threading.get_ident()
        self.native_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        self.root = Path(workspace)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.root / ".nx-mcp"
        self.state_dir.mkdir(exist_ok=True)
        self.stop_file = self.state_dir / "ui-stop"
        self.stop_file.unlink(missing_ok=True)
        self.mode = "auto"
        self.requested_mode = None
        self.busy = False
        self.stop_requested = False
        self.stopped = False
        self.ticks = 0
        self.completed = 0
        self.running_method = None
        self.operation_started = None
        self.last_duration_seconds = None
        self._snapshot = None
        self.last_method = None
        self.last_error = None
        self.started = time.time()
        self.resident = resident
        self.timer = None
        batch_descriptor = Path(os.environ.get("LOCALAPPDATA", "")) / "nx-mcp" / "bridge.json"
        if self.root.resolve() == default_workspace().resolve() and batch_descriptor.exists():
            raise RuntimeError(
                "Stop the batch bridge before attaching the interactive host to this workspace"
            )
        self.services = NXServices(
            self.session,
            NXOpen,
            nx_bridge.detect_nx_version(self.session),
            Workspace(self.root),
        )
        self.services.host = self  # nx_get_status reports the panel mode
        token = secrets.token_hex(32)
        progress = threading.Event()  # Set by bridge workers: a call queued or answered.
        self.dispatcher = MainThreadDispatcher(
            self.execute, queue_timeout=QUEUE_TIMEOUT, progress=progress, run_timeout=run_timeout
        )
        self.dispatcher.explain = self._explain_wait
        self.server = BridgeServer(
            self.dispatch,
            token=token,
            concurrent_requests=True,
            result_directory=Path(self.root) / ".nx-mcp" / "bridge-results",
            progress=progress,
        )
        self.descriptor_path = Path(descriptor_path)
        self.descriptor = BridgeDescriptor.create(
            self.server.port, self.services.build, token=token
        )
        self.main_hwnd = nx_main_window()
        self.window_disabled = False
        self.panel = ControlPanel(self)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.EnableWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.user.IsWindowEnabled.argtypes = [ctypes.c_void_p]
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = ctypes.c_short
        self._fast_accept = self._register_accept()
        if not self._fast_accept:
            self.server.start()  # Fallback: a worker thread accepts (slower, see module doc).
        self.timer_type = ctypes.WINFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_uint32
        )
        self.callback = self.timer_type(self.tick)
        self.user.SetTimer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint,
            self.timer_type,
        ]
        self.user.SetTimer.restype = ctypes.c_size_t
        self.user.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        if not resident:
            self.timer = self.user.SetTimer(None, 0, 100, self.callback)
            if not self.timer:
                raise ctypes.WinError(ctypes.get_last_error())
        self.descriptor.write(self.descriptor_path)

    # -- transport -------------------------------------------------------------

    def _register_accept(self):
        """Have Windows post WM_BRIDGE_ACCEPT to the panel when a request connects.

        A worker thread could not wake NX: it needs Python's GIL, which NX holds
        while idle. NX_MCP_FAST_WAKE=0 turns this off (accept thread instead)."""
        if os.environ.get("NX_MCP_FAST_WAKE") == "0":
            return False
        try:
            ws2 = ctypes.WinDLL("ws2_32")
            ws2.WSAAsyncSelect.argtypes = [
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_long,
            ]
            ws2.WSAAsyncSelect.restype = ctypes.c_int
            if ws2.WSAAsyncSelect(
                self.server.socket.fileno(), self.panel.hwnd, WM_BRIDGE_ACCEPT, FD_ACCEPT
            ):
                return False
            self._ws2 = ws2
            self.server.start(accept_thread=False)
            return True
        except Exception as exc:
            self.last_error = "Fast request wake-up unavailable: " + str(exc)
            return False

    def _prepare_socket(self, sock):
        """An accepted socket inherits FD_ACCEPT and non-blocking mode; undo both."""
        if self._ws2.WSAAsyncSelect(sock.fileno(), self.panel.hwnd, 0, 0):
            raise OSError("WSAAsyncSelect could not reset an accepted socket")
        sock.setblocking(True)

    def _accept(self):
        if self._fast_accept:
            self.server.accept_pending(self._prepare_socket)

    def bridge_ready(self):
        """FD_ACCEPT: a request arrived. Accept it and tick now instead of at the next timer."""
        try:
            self._accept()
        except Exception as exc:
            self.last_error = "Bridge accept: " + str(exc)
        if self.busy:
            self._rewake = True
        else:
            self.tick()

    def _wake(self):
        try:
            self.panel.wake()
        except Exception as exc:
            self.last_error = "Panel wake-up: " + str(exc)

    def _let_bridge_run(self, budget):
        """Give the bridge worker threads Python's GIL until none of them has work.

        NX holds the GIL while idle, so a worker only runs while a UI callback does.
        A worker wants to run when a connection is in flight but its caller is not
        parked on a queued call. Waiting on ``progress`` releases the GIL; it is set
        when a call is queued or an answer written. Bounded by ``budget``; a worker
        that makes no progress for a slice waits on its socket, not on us."""
        server, dispatcher = self.server, self.dispatcher
        progress = getattr(server, "progress", None)
        if progress is None:
            return
        deadline = time.monotonic() + budget
        while True:
            progress.clear()
            self._accept()
            active = server.in_flight - getattr(dispatcher, "parked", 0) > 0
            if not active and (self._fast_accept or not server.pending_connection()):
                return
            left = deadline - time.monotonic()
            if left <= 0 or not progress.wait(min(left, BRIDGE_SLICE)):
                return

    def dispatch(self, method, params):
        # Bridge-worker health reads use an immutable UI-thread snapshot only.
        # They must not wait behind the native operation they are diagnosing.
        snapshot = getattr(self, "_snapshot", None)
        blocked = self.blocked_by
        if blocked is not None and method != STATUS_TOOL:
            # NX waits for a person: no call can start, so none waits in the queue.
            raise self._blocked_error(blocked, "not_started")
        if method == STATUS_TOOL and snapshot and snapshot.get("running_method"):
            from nx_mcp.domains.session import status

            services = self.services
            # The queue as it is now (the snapshot's dates from the call's start).
            snapshot = {**snapshot, "queue": self._queue_state()}
            if blocked is not None:
                snapshot["activity"] = "blocked_by_dialog"
            envelope = status.envelope_from_snapshot(
                snapshot,
                params,
                root=services.workspace.root,
                build=getattr(services, "build", None),
                store=getattr(services, "store", None),
            )
            if blocked is not None:
                _show_blocked(envelope, blocked)
            return envelope
        return self.dispatcher.call(method, params)

    # -- panel -----------------------------------------------------------------

    def publish(self):
        state = self._refresh_snapshot()
        try:
            self._parts_lines = self.parts_lines()
        except Exception as exc:
            self.last_error = "Parts in use: " + str(exc)
        self._refresh_panel()
        temp = self.state_dir / "ui-state.tmp"
        temp.write_text(json.dumps(state))
        temp.replace(self.state_dir / "ui-state.json")

    def _refresh_snapshot(self):
        """Update the status snapshot that bridge workers answer health reads from."""
        state = self.status()
        self._snapshot = {
            **state,
            "parts": self._parts_snapshot(),
            "_sample_monotonic": time.monotonic(),
            "_operation_monotonic": self.operation_started,
        }
        return state

    def _parts_snapshot(self):
        """Work/display part for nx_get_status while a step runs (UI thread only)."""
        from nx_mcp.domains.session.status import snapshot_parts

        services = self.services
        session = getattr(services, "session", None)
        workspace = getattr(services, "workspace", None)
        if session is None or workspace is None:
            return None
        return snapshot_parts(session, workspace.root)

    def _refresh_panel(self):
        self.panel.update(*self.panel_state())
        show_parts = getattr(self.panel, "show_parts", None)
        if show_parts is not None:
            try:
                show_parts(list(self._parts_lines))
            except Exception as exc:
                self.last_error = "Panel parts: " + str(exc)
        show_grant = getattr(self.panel, "show_grant", None)
        if show_grant is not None:
            try:
                show_grant(self.grant_state())
            except Exception as exc:
                self.last_error = "Panel grant: " + str(exc)

    def grant_state(self):
        """The grant checkbox for the work part (production only), or None."""
        if self.running_method:
            return getattr(self.panel, "_grant", None)  # Keep it steady during a step.
        from nx_mcp.host.dispatch import grant_state

        try:
            return grant_state(self.services)
        except Exception:
            return None

    def _toggle_grant(self):
        """The engineer clicked "Allow agent to change <part>"."""
        from nx_mcp.host.dispatch import set_grant

        state = self.grant_state()
        if state is None:
            return
        allow = not state["granted"]
        name = set_grant(self.services, allow)
        self._set_notice(
            f"You allowed the agent to change {name}."
            if allow
            else f"The agent may no longer change {name}."
        )

    # -- "Parts in use" ----------------------------------------------------------------

    def parts_in_use(self):
        """The agent's loaded parts for the panel, in NX's order: ``{key, file, shown
        (shown / tab / background), state (modified / saved / new), last, at, ok}``;
        UI thread only. Never the engineer's own parts (``core/target.counts``)."""
        from nx_mcp.core import target
        from nx_mcp.kit import parts as kit_parts

        services = self.services
        session_parts = self.session.Parts
        tabs = kit_parts.tabs(session_parts)
        display = getattr(session_parts, "Display", None)
        rows = []
        for part in list(services.parts() or []):
            try:
                if not target.counts(services, part):
                    continue
                key = services.part_key(part)
                file = target.file_name(part)
                full = str(getattr(part, "FullPath", "") or "")
            except Exception:
                continue
            if kit_parts.same_part(part, display):
                shown = "shown"
            elif any(kit_parts.same_part(part, t) for t in tabs):
                shown = "tab"
            else:
                shown = "background"
            if bool(getattr(part, "IsModified", False)):
                state = "modified"
            else:
                state = "saved" if full and Path(full).is_file() else "new"
            last = self.activity.last_in(file)
            rows.append({"key": key, "file": file, "shown": shown, "state": state, **last})
        return rows

    def parts_lines(self):
        """``[(part key, line)]`` of "Parts in use" (``activity.part_line``)."""
        from nx_mcp.activity import part_line

        return [(row["key"], part_line(row)) for row in self.parts_in_use()]

    def _show_part(self, key):
        """The engineer clicked "Show part": activate that part's NX tab (undo marks
        kept, no Sheet Metal dialog). Never while the engineer is in an NX command."""
        from types import SimpleNamespace

        from nx_mcp.core import target
        from nx_mcp.kit import parts as kit_parts

        part = target.part_by_key(self.services, key)
        if part is None:
            self._set_notice("That part is no longer loaded.")
        elif self.engineer_busy():
            self._set_notice("Finish your NX command first, then click Show part again.")
        else:
            services = self.services
            tx = SimpleNamespace(
                session=self.session,
                nx=self.nx,
                uf=getattr(services, "uf", None),
                workspace=getattr(services, "workspace", None),
                services=services,
            )
            try:
                kit_parts.activate(tx, part)
                self._set_notice(f"Showing {target.file_name(part)} in its NX tab.")
            except Exception as exc:
                self._set_notice(f"NX did not show {target.file_name(part)}: {exc}")
        try:
            self._parts_lines = self.parts_lines()
        except Exception as exc:
            self.last_error = "Parts in use: " + str(exc)

    def _panel_part(self, result):
        """The part file a call worked in, for "Parts in use": ``envelope.part`` (two or
        more parts loaded), else the agent's only part; None when unknown."""
        envelope = result if isinstance(result, dict) else None
        row = envelope.get("part") if isinstance(envelope, dict) else None
        file = row.get("file") if isinstance(row, dict) else None
        if isinstance(file, str) and file:
            return file
        try:
            from nx_mcp.core import target

            agents = target.agent_parts(self.services)
            return target.file_name(agents[0]) if len(agents) == 1 else None
        except Exception:
            return None

    def _set_notice(self, text):
        self.notice = text
        self.notice_until = time.monotonic() + NOTICE_SECONDS

    def _notice_line(self):
        if self.notice and (self.notice_until is None or time.monotonic() < self.notice_until):
            return self.notice
        return None

    @property
    def activity(self):
        if self._activity is None:
            from nx_mcp.activity import Activity

            state_dir = getattr(self, "state_dir", None)
            self._activity = Activity(state_dir / "activity.log" if state_dir else None)
        return self._activity

    def history_lines(self, rows):
        """The latest activity lines, newest first."""
        return list(reversed(self.activity.recent(rows))) if rows > 0 else []

    def _with_history(self, lines):
        return "\n".join(lines + self.history_lines(self.panel_rows - len(lines)))

    def _summary_of(self, pending):
        """The call's panel line: its target part's file first while two or more parts
        are loaded (``[cover.prt] Extrude ...``), computed once."""
        if pending.summary is None:
            from nx_mcp.activity import with_part

            pending.summary = with_part(
                self._target_file(pending.method, pending.params),
                self._summarize(pending.method, pending.params),
            )
        return pending.summary

    def _target_file(self, method, params):
        """The target part's file of a call while two or more parts are loaded, else
        None; a courtesy that never fails a call (``dispatch.target_file``). A
        development snippet (``nx_dev_python``) acts anywhere: None, never the work part."""
        try:
            from nx_mcp.host.dispatch import target_file

            if method == DEV_PYTHON:
                return None

            return target_file(self.services, method, params)
        except Exception:
            return None

    def _ran_in(self, result, error):
        """After a call: the file its envelope names (``envelope.part``) while two or
        more parts are loaded, else the target found before it ran."""
        envelope = result if isinstance(result, dict) else None
        details = getattr(error, "details", None)
        if envelope is None and isinstance(details, dict):
            envelope = details.get("envelope")
        row = envelope.get("part") if isinstance(envelope, dict) else None
        file = row.get("file") if isinstance(row, dict) else None
        if isinstance(file, str) and file:
            try:
                if len(list(self.services.parts() or [])) >= 2:
                    return file
            except Exception:
                pass
        return self.current_part

    def panel_text(self):
        from nx_mcp.activity import LINE_WIDTH, _clip

        if self.running_method:
            # The panel cannot repaint while NX runs this step, so show a start
            # time rather than a counter that would appear frozen.
            started = time.strftime("%H:%M:%S", time.localtime(self.current_started_at))
            head, tail = f"▶  {started}  ", _running_tail(self.running_method)
            from nx_mcp.activity import with_part

            summary = with_part(self.current_part, self.current_summary or self.running_method)
            lines = [head + _clip(summary, LINE_WIDTH - len(head) - len(tail)) + tail]
            if self.current_intent:
                indent = " " * (len(head) + 2)
                lines.append(indent + _clip(f"“{self.current_intent}”", LINE_WIDTH - len(indent)))
            return self._with_history(lines)
        if self.waiting_count:
            # The card shows one call; an answer applies to that call only.
            lines = [f"▶ Agent wants to: {self.waiting_summary}"]
            if self.waiting_intent:
                lines.append(f"   “{self.waiting_intent}”  (agent's note)")
            left = max(0, int(self._hold_left()))
            more = self.waiting_count - 1
            scope = f" for this step ({more} more waiting)" if more else ""
            lines.append(f"Answer within {left} s{scope}. Finish your current NX command first.")
            return "\n".join(lines)
        if self.busy_wait is not None:
            pending = self.busy_wait
            waited = int(time.monotonic() - (pending.seen_at or time.monotonic()))
            head = f"⏸  Waiting until you finish your command ({waited} s): "
            lines = [head + _clip(self._summary_of(pending), LINE_WIDTH - len(head))]
            intent = pending.params.get("intent")
            if intent:
                lines.append("    " + _clip(f"“{intent}”", LINE_WIDTH - 4))
            return self._with_history(lines)
        if self.mode == "ask":
            idle = "·  Ask me first — agent steps wait for your OK here"
        else:
            idle = "·  Auto — you work in NX, agent steps run between your commands"
        return self._with_history([self._notice_line() or idle])

    def panel_state(self):
        """Body text, window title, the checked mode and the request card's buttons."""
        product = "NX MCP DEV" if self.dev else "NX MCP"
        label = MODE_LABELS.get(self.mode, self.mode)
        waiting = bool(self.waiting_count) or self.busy_wait is not None
        if self.mode == "ask" and self.waiting_count:
            title = f"{product} — Agent is waiting"
        elif self.running_method:
            title = f"{product} — {label} · agent step running"
        elif waiting:
            title = f"{product} — {label} · agent waiting for you"
        else:
            title = f"{product} — {label}"
        # Only Ask me first holds calls for an answer: Auto never shows a card.
        card = "ask" if self.waiting_count and not self.requested_mode else None
        return self.panel_text(), title, self.requested_mode or self.mode, card

    def status(self):
        waiting_command = 1 if self.busy_wait is not None else 0
        if self.running_method:
            activity = "running"
        elif self.waiting_count:
            activity = "waiting_for_answer"
        elif waiting_command:
            activity = "waiting_for_engineer_command"
        else:
            activity = "idle"
        return {
            "interactive": True,
            "pid": os.getpid(),
            "mode": self.mode,
            "mode_label": MODE_LABELS.get(self.mode, self.mode),
            "modes": list(MODES),
            "activity": activity,
            "running_method": self.running_method,
            "running_operation_id": (
                getattr(self, "current_operation_id", None) if self.running_method else None
            ),
            "blocked_by_dialog": self.blocked_by,
            "operation_elapsed_seconds": (
                time.monotonic() - self.operation_started
                if self.operation_started is not None
                else None
            ),
            "last_duration_seconds": self.last_duration_seconds,
            "sampled_at": time.time(),
            "snapshot_only": False,
            "pause_semantics": "between operations; does not interrupt a native call",
            "actual_ui_lock": self.ui.AskLockStatus() == self.nx.UI.Status.Lock,
            "native_lock_value": str(self.ui.AskLockStatus()),
            "can_open_part": self.ui.CanOpenPart(),
            "nx_window_input_enabled": bool(self.user.IsWindowEnabled(self.main_hwnd)),
            "engineer_busy": self.engineer_busy(),
            "main_thread_id": self.native_thread,
            "callback_thread_id": ctypes.windll.kernel32.GetCurrentThreadId(),
            "ticks": self.ticks,
            "completed_operations": self.completed,
            "last_method": self.last_method,
            "last_error": self.last_error,
            "waiting_for_engineer": self.waiting_count,
            "waiting_for_engineer_command": waiting_command,
            "waiting_seconds": (
                time.monotonic() - self.waiting_since if self.waiting_since is not None else None
            ),
            "queue": self._queue_state(),
            "uptime_seconds": time.time() - self.started,
            "request_wake": "fd_accept" if self._fast_accept else "timer",
            "scheduler": {
                "journal": "resident journal loop; one queued call per tick",
                "startup": "NX startup application timer on the UI thread; one queued call per tick",
            }.get(self.driver, "Win32 UI-thread timer; one queued call per tick"),
            "driver": self.driver,
        }

    def _queue_state(self):
        """``{waiting, oldest_seconds}``: agent calls queued for NX (not the status
        read) and how long the oldest has waited."""
        try:
            held = [p for p in self.dispatcher.waiting() if not self._never_waits(p)]
        except Exception:
            return {"waiting": 0, "oldest_seconds": None}
        if not held:
            return {"waiting": 0, "oldest_seconds": None}
        oldest = min(p.queued_at for p in held)
        return {"waiting": len(held), "oldest_seconds": round(time.monotonic() - oldest, 1)}

    # -- panel mode ------------------------------------------------------------

    def set_mode(self, mode):
        """The engineer picked a panel mode (a radio button or "Run & switch to Auto").
        The choice is kept across development reloads (ui-mode.json)."""
        if threading.get_ident() != self.thread:
            raise RuntimeError("UI mode change called off NX thread")
        mode = MODE_ALIASES.get(mode, mode)
        if mode not in MODES:
            raise NXError("NX_INVALID_ARGUMENT", "mode must be auto or ask (manual = ask)")
        self.mode = mode
        self._remember_mode(mode)

    def _remember_mode(self, mode):
        try:
            (Path(self.state_dir) / "ui-mode.json").write_text(
                json.dumps({"pid": os.getpid(), "mode": mode}), encoding="utf-8"
            )
        except (OSError, TypeError, AttributeError) as exc:
            self.last_error = "Cannot remember the panel mode: " + str(exc)

    def _record_release_error(self, step, exc):
        message = f"{step}: {exc}"
        self.last_error = message if not self.last_error else self.last_error + "; " + message

    def _release_to_engineer(self):
        """Re-enable NX's main window after a call; never raises."""
        if self.window_disabled:
            try:
                self.user.EnableWindow(self.main_hwnd, True)
                self.window_disabled = False
            except Exception as exc:
                self._record_release_error("Cannot re-enable the NX window", exc)

    def _take_light(self):
        """Take NX for one call: disable its main window (no LockAccess). Returns
        whether this call disabled it; a modal dialog's disabled window is left alone."""
        if self.window_disabled:
            return False
        try:
            if not self.user.IsWindowEnabled(self.main_hwnd):
                return False
            self.user.EnableWindow(self.main_hwnd, False)
            self.window_disabled = True
            return True
        except Exception as exc:
            self._record_release_error("Cannot disable the NX window", exc)
            return False

    # -- engineer activity -----------------------------------------------------

    def busy_signals(self):
        """What says the engineer is in the middle of something in NX (each ~µs)."""
        signals = {"simulated": bool(self.simulated_busy)}

        def probe(name, read):
            try:
                signals[name] = bool(read())
            except Exception:
                signals[name] = False

        probe("ui_locked", lambda: self.ui.AskLockStatus() == self.nx.UI.Status.Lock)
        probe("command_active", lambda: not self.ui.CanOpenPart())
        if not self.window_disabled:  # A modal dialog disables NX's main window.
            probe("modal_dialog", lambda: not self.user.IsWindowEnabled(self.main_hwnd))
        probe("menu_or_drag", self._gui_thread_busy)
        probe(
            "mouse_button_down",
            lambda: any(self.user.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4)),
        )
        return signals

    def engineer_busy(self):
        return any(self.busy_signals().values())

    def _gui_thread_busy(self):
        """A menu, a window move/size or a mouse capture (dragging) on NX's UI thread."""
        from ctypes import wintypes as w

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

        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        if not self.user.GetGUIThreadInfo(self.native_thread, ctypes.byref(info)):
            return False
        # GUI_INMOVESIZE 2, GUI_INMENUMODE 4, GUI_SYSTEMMENUMODE 8, GUI_POPUPMENUMODE 16
        return bool(info.flags & 0x1E or info.hwndCapture)

    def model_fingerprint(self):
        """What any engineer edit changes: the newest undo mark, the number of undo
        marks, the loaded parts and the work and display parts (~0.03 ms)."""
        session = self.session

        def probe(read):
            try:
                return read()
            except Exception:
                return None

        def tag(part):
            return int(part.Tag) if part else 0

        values = (
            probe(lambda: session.GetNewestUndoMark(self.nx.Session.MarkVisibility.AnyVisibility)),
            probe(
                lambda: self._uf_session().Undo.AskNumberOfMarks(
                    self.nx.UF.Undo.UserVisibility.ANY_VIS
                )
            ),
            probe(lambda: tuple(int(p.Tag) for p in session.Parts)),
            probe(lambda: tag(session.Parts.Work)),
            probe(lambda: tag(session.Parts.Display)),
        )
        return None if all(v is None for v in values) else values

    def _uf_session(self):
        if self._uf is None:
            self._uf = self.nx.UF.UFSession.GetUFSession()
        return self._uf

    def _remember_model(self):
        self._fingerprint = self.model_fingerprint()
        self._has_fingerprint = True

    def _check_engineer_change(self):
        """Before an agent call: did the engineer change the model since the last one?
        Only then are the agent's references and checkpoints invalidated."""
        if not self._engineer_had_nx:
            return False
        self._engineer_had_nx = False
        if not self._has_fingerprint:
            return False  # No agent call yet: no references or checkpoints to protect.
        now = self.model_fingerprint()
        changed = _edit_changes(self._fingerprint, now)
        if not changed:
            self._fingerprint = now  # a display, work part or tab change: no edit
            return False
        self._invalidate_references(_display_changed(self._fingerprint, now))
        try:
            self.activity.engineer_change(detail=", ".join(changed))
        except Exception as exc:
            self.last_error = "Activity history: " + str(exc)
        return True

    def _invalidate_references(self, display_changed=False):
        """Manual edits have no bridge receipt: a later agent rollback must not undo
        them. References are re-validated: refs of objects the edit deleted go stale,
        the others (and every loaded part's ref) stay, because a ref follows NX's
        persistent object identity (``NXServices.revalidate``); the fingerprint can
        also change without an edit (NX's own marks after a long export). Never raises."""
        try:
            self.services.revalidate()
        except Exception as exc:
            self._record_release_error("Cannot re-validate references", exc)
            self._drop_references()
        try:
            # undo history, checkpoints (display_reset when a display switch dropped
            # NX's undo marks), review epoch
            self.services.engineer_changed(display_changed)
        except Exception as exc:
            self._record_release_error("Cannot clear rollback history", exc)

    def _drop_references(self):
        """Drop every reference of the open parts (when re-validation failed)."""
        try:
            for part in self.session.Parts:
                try:
                    self.services.objects.invalidate_part(self.services.part_key(part))
                except Exception as exc:
                    self._record_release_error("Cannot invalidate part references", exc)
        except Exception as exc:
            self._record_release_error("Cannot list open parts", exc)
            # Without the part list, drop every reference rather than keep stale ones.
            try:
                objects = self.services.objects
                for part_id in {e.reference.part_id for e in objects._objects.values()}:
                    objects.invalidate_part(part_id)
            except Exception as exc2:
                self._record_release_error("Cannot clear references", exc2)

    # -- execution -------------------------------------------------------------

    def execute(self, method, params):
        from nx_mcp.core import catalogue
        from nx_mcp.host import dispatch

        if threading.get_ident() != self.thread:
            raise RuntimeError("NX request called off registering UI thread")
        if method == STATUS_TOOL:  # never waits for the engineer
            return dispatch.execute(self.services, method, params)
        if not self._light:
            raise NXError(
                "NX_CONTROL_PAUSED",
                "The engineer has NX and has not approved this step. Ask them to answer "
                "the request on the NX MCP panel or to pick Auto.",
                details={"mutation_outcome": "not_started"},
            )
        if self.driver == "journal" and not _read_only(method) and not self._nx_application_ready():
            # Seen on NX 2406: the first part creation in a fresh session must
            # start NX's Modeling application, which waits for NX's own main
            # loop - held by this host - and freezes NX for good. Refuse instead.
            raise NXError(
                "NX_NO_WORK_PART",
                "NX has no part open yet. In NX, create or open any part once "
                "(File > New or File > Open), then retry.",
                details={"mutation_outcome": "not_started"},
            )
        self._check_engineer_change()
        self.last_method = method
        self.running_method = method
        self.operation_started = time.monotonic()
        # Summarize before running: a delete removes the names it would show.
        self.current_summary = self._summarize(method, params)
        self.current_part = self._target_file(method, params)
        self.current_intent = params.get("intent")
        self.current_operation_id = params.get("operation_id")
        self.current_started_at = time.time()
        self.last_error = None
        result = error = None
        watch = None
        try:
            try:
                self.publish()
            except Exception as exc:
                self.last_error = "UI status publication failed: " + str(exc)
            watch = self._watch_dialogs(refuse=not _read_only(method))
            result = dispatch.execute(self.services, method, params)
            self._report_dialogs(result, watch.stop())
            watch = None
            self.completed += 1
            part = self.session.Parts.Display
            sheet = getattr(getattr(part, "DrawingSheets", None), "CurrentDrawingSheet", None)
            from nx_mcp.kit.drawing import work_view_is_drawing as drawing_work_view

            if part and not _read_only(method) and sheet is None and not drawing_work_view(part):
                try:
                    part.ModelingViews.WorkView.UpdateDisplay()
                except Exception as exc:
                    warning = "View refresh: " + str(exc)
                    if "ok" in result:  # A catalogue envelope: warnings are {code, message}.
                        warning = {"code": "NX_UPDATE_WARNING", "message": warning}
                    result.setdefault("warnings", []).append(warning)
            return result
        except BaseException as raised:
            problem = raised
            if not isinstance(raised, Exception):
                # SystemExit, KeyboardInterrupt...: an error result for this call only,
                # never an escape into NX's message loop.
                problem = NXError(
                    "NX_BUILD_FAILED",
                    f"{type(raised).__name__} raised by {method}: {raised}",
                    details={"mutation_outcome": "unknown"},
                )
            self.last_error = str(problem)
            error = problem
            if watch is not None:
                self._report_dialogs(None, watch.stop(), error=problem)
            if problem is raised:
                raise
            raise problem from raised
        finally:
            self.last_duration_seconds = time.monotonic() - self.operation_started
            try:
                self.activity.record(
                    method,
                    self.current_summary,
                    started=self.current_started_at,
                    duration=self.last_duration_seconds,
                    intent=self.current_intent,
                    read_only=_model_unchanged(method),
                    result=result,
                    error=error,
                    part=self._ran_in(result, error),
                )
                if catalogue.get(method) is not None:
                    self.activity.touch(
                        self._panel_part(result if result is not None else _envelope(error)),
                        self.current_summary,
                        started=self.current_started_at,
                        ok=error is None,
                    )
            except Exception as exc:
                self.last_error = "Activity history: " + str(exc)
            self.running_method = None
            self.operation_started = None
            self.blocked_by = None  # the call ended: no dialog holds it any more
            self.current_summary = self.current_intent = self.current_started_at = None
            self.current_part = None
            self._remember_model()
            try:
                self.publish()
            except Exception as exc:
                # A failed diagnostic write must not turn a committed operation
                # into a failure response that invites an unsafe retry.
                self.last_error = "UI status publication failed: " + str(exc)

    # -- modal NX dialogs (host/dialogs.py) ---------------------------------------

    def _watch_dialogs(self, refuse=False):
        """Watch for modal NX dialogs a call opens; ``refuse`` a call while one is open.

        Every non-read call is refused (``NX_MODAL_DIALOG_OPEN``, nothing started) when
        a modal NX dialog is open as it starts. The busy wait came first: an engineer's
        command or dialog delays the call up to BUSY_WAIT_SECONDS (``engineer_busy``);
        this scan is the last check after the host took NX, so a dialog that opened in
        between, or one the busy probes miss (a dialog owned by another NX window), can
        never have a model, view or file step run under it. Reads are never refused.
        """
        from nx_mcp.host import dialogs

        def scan():
            # NX's main window disabled by the host for this call proves no modality.
            disabled = (self.main_hwnd,) if getattr(self, "window_disabled", False) else ()
            return dialogs.scan(host_disabled=disabled)

        open_now = scan()
        if open_now and refuse:
            raise NXError(
                "NX_MODAL_DIALOG_OPEN",
                dialogs.message(open_now, still_open=True),
                hint="Ask the engineer to answer the NX dialog, then retry",
                details={"mutation_outcome": "not_started",
                         "dialogs": [d.row() for d in open_now]},
            )  # fmt: skip
        return dialogs.Watch(
            open_now, on_new=self._dialog_appeared, on_gone=self._dialog_gone, scanner=scan
        ).start()

    def _dialog_appeared(self, dialog):
        """From the watch thread, while NX waits for an answer: log the dialog, mark NX
        blocked (``nx_get_status`` shows it; new calls are refused at once), answer every
        queued call, and in development the blocked call too."""
        try:
            self.activity.nx_dialog(
                f"NX waits for an answer to a modal dialog (during "
                f"{self.running_method or 'an agent step'}): {dialog.describe()}"
            )
        except Exception as exc:
            self.last_error = "Activity history: " + str(exc)
        if self.blocked_by is None:
            self.blocked_by = {
                **dialog.row(),
                "hwnd": dialog.hwnd,
                "tool": self.running_method,
                "part": getattr(self, "current_part", None),
                "since": time.time(),
            }
            self._mark_snapshot(self.blocked_by)
            self._reject_queued(self.blocked_by)
        if getattr(self, "dev", False):
            self._answer_blocked_call(dialog)

    def _dialog_gone(self, dialog):
        """From the watch thread: someone answered the blocking dialog; NX runs again."""
        blocked = self.blocked_by
        if blocked is not None and blocked.get("hwnd") == dialog.hwnd:
            self.blocked_by = None
            self._mark_snapshot(None)

    def _mark_snapshot(self, blocked):
        """Put the blocking dialog in (or out of) the snapshot health reads answer from."""
        snapshot = getattr(self, "_snapshot", None)
        if isinstance(snapshot, dict):
            self._snapshot = {**snapshot, "blocked_by_dialog": blocked}

    def _reject_queued(self, blocked):
        """Answer every queued call now: none can start before someone answers the dialog."""
        try:
            self.dispatcher.reject_where(
                lambda pending: pending.method != STATUS_TOOL,
                self._blocked_error(blocked, "not_started"),
            )
        except Exception as exc:
            self.last_error = "Cannot answer the queued calls: " + str(exc)

    def _blocked_error(self, blocked, outcome):
        """NX_MODAL_DIALOG_OPEN for a call NX cannot run or finish while ``blocked``."""
        from nx_mcp.host import dialogs

        since = blocked.get("since")
        row = {k: blocked.get(k) for k in ("title", "text", "buttons")}
        then = (
            "then query nx_get_operation before retrying"
            if outcome == "unknown"
            else "nx_get_status shows when NX is free; then retry"
        )
        return NXError(
            "NX_MODAL_DIALOG_OPEN",
            dialogs.blocked_message(blocked, outcome=outcome),
            hint="Ask the engineer to answer the NX dialog (the agent never clicks it); " + then,
            details={
                "mutation_outcome": outcome,
                "dialogs": [row],
                "blocked_call": {
                    "tool": blocked.get("tool"),
                    "part": blocked.get("part"),
                    "seconds": round(time.time() - since) if since else None,
                },
            },
        )

    def _explain_wait(self, pending, started):
        """The dispatcher's timeout answer while a modal dialog blocks NX (else None)."""
        blocked = self.blocked_by
        if blocked is None:
            return None
        return self._blocked_error(blocked, "unknown" if started else "not_started")

    def _answer_blocked_call(self, dialog):
        """Development only: answer the call a new modal dialog blocks with an error now.

        The call itself stays in NX until someone answers the dialog (never the agent);
        the agent learns at once instead of waiting for the bridge timeout. Production
        only reports the dialog (``_report_dialogs``); its caller hears of it at the
        run timeout (``_explain_wait``) unless someone answers first."""
        blocked = self.blocked_by or {
            **dialog.row(),
            "tool": self.running_method,
            "since": time.time(),
        }
        try:
            self.dispatcher.fail_running(self._blocked_error(blocked, "unknown"))
        except Exception as exc:
            self.last_error = "Cannot answer the blocked call: " + str(exc)

    def _report_dialogs(self, result, seen, error=None):
        """Name the dialogs a call opened in its result (or error); never clicks them."""
        if not seen:
            return
        from nx_mcp.host import dialogs

        disabled = (self.main_hwnd,) if getattr(self, "window_disabled", False) else ()
        still = {d.hwnd for d in dialogs.scan(host_disabled=disabled)}
        groups = [
            (True, [d for d in seen if d.hwnd in still]),
            (False, [d for d in seen if d.hwnd not in still]),
        ]
        rows = [{**d.row(), "open": d.hwnd in still} for d in seen]
        for still_open, group in groups:
            if not group:
                continue
            text = dialogs.message(group, still_open=still_open)
            self._set_notice(text)
            with contextlib.suppress(Exception):
                self.activity.nx_dialog(text)
            warning = {"code": "NX_MODAL_DIALOG", "message": text}
            if isinstance(result, dict):
                if "ok" not in result:  # a development tool: warnings are text
                    warning = text
                result.setdefault("warnings", []).append(warning)
            details = getattr(error, "details", None)
            if isinstance(details, dict):
                details["modal_dialogs"] = rows
                envelope = details.get("envelope")
                if isinstance(envelope, dict):
                    envelope.setdefault("warnings", []).append(warning)

    def tick(self, *args):
        if self.busy or self.stopped:
            return
        self.busy = True
        ran = False
        try:
            self.ticks += 1
            if self.stop_requested or self.stop_file.exists():
                self.stop()
                return
            before = (self.mode, self.waiting_count)
            if self.requested_mode:
                mode, self.requested_mode = self.requested_mode, None
                try:
                    self.set_mode(mode)
                except NXError as exc:
                    self.last_error = str(exc)
            if self.open_log_requested:
                self.open_log_requested = False
                self._open_log()
            if self.grant_toggle_requested:
                self.grant_toggle_requested = False
                self._toggle_grant()
            if self.show_part_requested is not None:
                key, self.show_part_requested = self.show_part_requested, None
                self._show_part(key)
            self._apply_answer()
            self._let_bridge_run(ARRIVAL_BUDGET)
            ran = self._schedule()
            if ran:
                # Before the answer goes out: the agent's next health read must see
                # NX released (Auto) and the mode as it is now.
                try:
                    self._refresh_snapshot()
                except Exception as exc:
                    self.last_error = "UI status snapshot failed: " + str(exc)
                self._let_bridge_run(REPLY_BUDGET)
            if ran or self.ticks % 10 == 0 or before != (self.mode, self.waiting_count):
                # The status snapshot answers health reads: keep it current once NX
                # is released after a step and when the mode changes.
                try:
                    self.publish()
                except OSError as exc:
                    self.last_error = "UI status publication failed: " + str(exc)
            else:
                self._refresh_panel()
        except BaseException as exc:
            self.last_error = str(exc)
            # Last-resort native callback cleanup must not escape.
            with contextlib.suppress(BaseException):
                self._release_to_engineer()
        finally:
            self.busy = False
            if ran or self._rewake:
                # Another call may be queued: tick again as soon as NX dispatches.
                self._rewake = False
                self._wake()

    # -- scheduling ------------------------------------------------------------

    def _never_waits(self, pending):
        """The status read never waits: not for a card, not for a busy engineer."""
        return pending.method == STATUS_TOOL

    def _needs_answer(self, pending):
        """Waits for the engineer's answer on the request card: Ask me first only."""
        return self.mode == "ask" and not pending.approved

    def _schedule(self):
        """Run at most one agent call; True when one ran."""
        if self.dispatcher.drain_where(self._never_waits):
            return True
        self._reject_gone()
        held = [p for p in self.dispatcher.waiting() if not self._never_waits(p)]
        if not held:
            self._clear_waiting()
            self.busy_wait = None
            return False
        now = time.monotonic()
        for pending in held:
            if pending.seen_at is None:
                pending.seen_at = now
        card = [p for p in held if self._needs_answer(p)]
        if card:
            self._show_card(card)  # May expire the shown call: it is answered.
        else:
            self._clear_waiting()
        if self.mode == "auto":
            # Reads never wait: not for a busy engineer, not behind a card. They
            # leave NX's window alone while the engineer is busy (a disabled window
            # would cancel a drag or a menu).
            read = next((p for p in held if self._unasked_read(p)), None)
            if read is not None:
                self.busy_wait = None
                return self._run_light(read, take=not self.engineer_busy())
        # Calls run in order, except that a call needing no answer (approved on the
        # card) never waits behind one that does.
        first = next(
            (p for p in held if not self._needs_answer(p) and not p.complete.is_set()), None
        )
        if first is None:
            self.busy_wait = None
            return False
        if not _read_only(first.method) and self.engineer_busy():
            return self._wait_for_engineer([first])
        self.busy_wait = None
        return self._run_light(first)

    def _gone(self, pending):
        """Whether a queued call's client gave up: it closed its socket (an MCP
        cancellation), or the call changes something and its ``expires_at`` passed."""
        from nx_mcp.bridge import peer_closed
        from nx_mcp.core import target

        if peer_closed(getattr(pending, "peer", None)):
            return True
        if _read_only(pending.method):
            return False
        try:
            expires = target.check_call_keys(None, pending.params.get("expires_at"))
        except Exception:
            return False  # malformed: the kernel answers it with the field
        return expires is not None and time.time() > expires

    def _reject_gone(self):
        """Answer queued calls whose client gave up before they are shown or run; the
        activity log says so (one line per call)."""
        from nx_mcp.bridge import client_gone

        gone = [p for p in self.dispatcher.waiting() if self._gone(p)]
        if not gone:
            return
        ids = {id(p) for p in gone}
        self.dispatcher.reject_where(lambda p: id(p) in ids, client_gone())
        for pending in gone:
            self._log_skipped(pending)

    def _log_skipped(self, pending):
        """``Skipped [b.prt] Extrude ...: the agent stopped waiting``: a queued call the
        host or the bridge answered NX_CALL_EXPIRED without running it."""
        error = getattr(pending, "error", None)
        if getattr(pending, "started", False) or getattr(error, "code", None) != "NX_CALL_EXPIRED":
            return
        try:
            self.activity.skipped(self._summary_of(pending), method=pending.method)
        except Exception as exc:
            self.last_error = "Activity history: " + str(exc)

    def _unasked_read(self, pending):
        return (
            not pending.complete.is_set()
            and _read_only(pending.method)
            and not self._needs_answer(pending)
        )

    def _run_light(self, pending, take=True):
        """Auto and approved steps: take NX for this call only, then give it back."""
        took = False
        try:
            took = take and self._take_light()
            self._light = True
            ran = bool(self.dispatcher.drain_where(lambda p: p is pending))
            self._log_skipped(pending)  # the bridge found its client gone at the start
            return ran
        finally:
            self._light = False
            if took:
                self._release_to_engineer()
            self._engineer_had_nx = True

    def _wait_for_engineer(self, pending_calls):
        """The engineer is busy in NX: wait, at most BUSY_WAIT_SECONDS per call."""
        now = time.monotonic()
        expired = {id(p) for p in pending_calls if now - p.seen_at >= BUSY_WAIT_SECONDS}
        if expired:
            span = _span(BUSY_WAIT_SECONDS)
            self.dispatcher.reject_where(
                lambda p: id(p) in expired,
                NXError(
                    "NX_UI_BUSY",
                    f"The engineer was busy in an NX command for {span}, so the step did "
                    "not run. Retry later, or ask in chat when NX is free.",
                    retryable=True,
                ),
            )
            self._set_notice(f"An agent step was not run: you were busy in NX for {span}.")
            self.busy_wait = None
            return False
        self.busy_wait = pending_calls[0]
        self._summary_of(self.busy_wait)
        return False

    def _show_card(self, waiting):
        """Show the oldest call waiting for an answer, on its own clock: the clock
        (``waiting_since``) starts when the card shows that call."""
        now = time.monotonic()
        shown = waiting[0]
        if self.waiting_since is None:
            self.notice = None
            self._flash_nx()
        if not self.card or self.card[0] is not shown:
            if shown.shown_at is None:
                shown.shown_at = now
            self.waiting_since = shown.shown_at
        self.card = (shown,)
        if self._hold_left(now) <= 0:
            self._reject_card(
                NXError(
                    "NX_CONTROL_PAUSED",
                    f"The engineer did not answer within {HOLD_SECONDS} s. Ask them in chat, "
                    "or ask them to answer the request on the NX MCP panel.",
                    retryable=True,
                ),
                f"The agent's request expired after {HOLD_SECONDS} s without an answer.",
            )
            return
        self.waiting_count = len(waiting)
        self.waiting_summary = self._summary_of(shown)
        self.waiting_intent = shown.params.get("intent")

    def _hold_left(self, now=None):
        """Seconds the shown call may still wait for an answer: its own HOLD_SECONDS,
        ending a little before the bridge stops waiting for it (QUEUE_TIMEOUT)."""
        from nx_mcp.bridge import QUEUE_TIMEOUT

        now = time.monotonic() if now is None else now
        if self.waiting_since is None:
            return HOLD_SECONDS
        left = HOLD_SECONDS - (now - self.waiting_since)
        if self.card:
            queued = getattr(self.card[0], "queued_at", None)
            if isinstance(queued, (int, float)):
                left = min(left, QUEUE_TIMEOUT - 5 - (now - queued))
        return left

    def _reject_card(self, error, notice):
        """Answer the call the card shows (only that one) with ``error``."""
        shown = {id(p) for p in self.card}
        self.dispatcher.reject_where(lambda p: id(p) in shown, error)
        self._clear_waiting()
        self._set_notice(notice)

    def _apply_answer(self):
        """The engineer answered the request card: Run once, Run & switch to Auto, Not now."""
        answer, self.answer_requested = self.answer_requested, None
        if not answer or not self.card:
            return
        if answer == "decline":
            self._reject_card(
                NXError(
                    "NX_CONTROL_DECLINED",
                    "The engineer chose 'Not now' in the NX MCP control window. "
                    "Ask in chat before requesting this again.",
                ),
                "You declined the agent's request.",
            )
            return
        for pending in self.card:  # the shown call only
            pending.approved = True
        self._clear_waiting()
        if answer == "run_auto":
            self.set_mode("auto")  # the calls still queued run under Auto's rules

    def _clear_waiting(self):
        self.card = ()
        self.waiting_since = None
        self.waiting_count = 0
        self.waiting_summary = None
        self.waiting_intent = None

    def _summarize(self, method, params):
        from nx_mcp.core.summary import humanize, relative_path

        try:
            from nx_mcp.host.dispatch import render_summary

            line = render_summary(self.services, method, params, path_of=relative_path(self.root))
            if line is not None:
                return line
        except Exception:
            pass  # A summary is a courtesy; it must never stop a request from waiting.
        return humanize(method)

    def _nx_application_ready(self):
        """True once NX runs an application with at least one part loaded."""
        try:
            application = str(self.session.ApplicationName or "")
            loaded = any(True for _ in self.session.Parts)
        except Exception:
            return True  # Unknown: do not block work on a failed probe.
        return loaded and application not in {"", "UG_APP_NOPART"}

    def _dev_python(self, code):
        """Run a Python snippet on the NX UI thread (development launcher only).

        Names: NXOpen, session, part (work part), services, resolve(id). Set
        `result` to return a value; print(), stderr and listing-window output are
        returned as stdout. A raised snippet is undone to the call's mark and the
        display and work parts it started with are shown again (dispatch.run_dev_tool).
        """
        import contextlib
        import io

        namespace = {
            "NXOpen": self.nx,
            "session": self.session,
            "part": self.session.Parts.Work,
            "services": self.services,
            "resolve": lambda ref: self.services.resolve(ref),
        }
        output = io.StringIO()
        # print() and tracebacks/warnings written to stderr come back in the result,
        # never in NX's listing window; ListingWindow writes go to a file meanwhile
        # (dispatch.execute -> host/quiet.py) and are appended to stdout.
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            exec(compile(code, "<nx_dev_python>", "exec"), namespace)  # noqa: S102 - dev only
        return {
            "status": "success",
            "stdout": output.getvalue()[-20000:],
            "result": repr(namespace.get("result"))[:20000],
        }

    def _open_log(self):
        log = self.activity.log_path
        if log is None or not log.exists():
            self._set_notice("No agent activity logged yet.")
            return
        try:
            os.startfile(log)  # Opens in the engineer's default text editor.
        except OSError as exc:
            self._set_notice(f"Cannot open {log}: {exc}")

    def _flash_nx(self):
        """Flash NX's taskbar button until NX is in front; never steal focus."""
        from ctypes import wintypes as w

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", w.UINT),
                ("hwnd", w.HWND),
                ("dwFlags", w.DWORD),
                ("uCount", w.UINT),
                ("dwTimeout", w.DWORD),
            ]

        try:
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), self.main_hwnd, 0x2 | 0xC, 3, 0)
            self.user.FlashWindowEx(ctypes.byref(info))
        except Exception as exc:
            self.last_error = "Cannot flash NX: " + str(exc)

    def stop(self):
        from nx_mcp.bridge import BridgeDescriptor

        self._release_to_engineer()  # NX's window is never left disabled.
        self.stopped = True
        # Each teardown step runs even if an earlier one fails.
        for step, action in (
            ("Kill timer", lambda: self.timer and self.user.KillTimer(None, self.timer)),
            ("Stop dispatcher", self.dispatcher.stop),
            ("Stop bridge server", self.server.stop),
            ("Close panel", self.panel.close),
        ):
            try:
                action()
            except Exception as exc:
                self._record_release_error(step, exc)
        try:
            if BridgeDescriptor.read(self.descriptor_path).token == self.descriptor.token:
                self.descriptor_path.unlink()
        except (ValueError, OSError):
            pass


def start(workspace, descriptor_path, resident=False, *, dev=False, driver=None):
    """Start the host. The panel mode is the one last chosen in this NX process
    (kept across development reloads), else Auto, in production and in the
    development loop alike. dev marks the panel of the development loop. driver names what ticks the host: "timer"
    (its own Win32 timer), "journal" (a resident journal loop) or "startup" (an
    NX startup application); resident hosts default to "journal"."""
    global _host
    if _host is not None and not _host.stopped:
        return _host.status()
    if _host is not None:
        _retired.append(_host)  # Keep native callback delegates alive until NX exits.
    candidate = InteractiveHost.__new__(InteractiveHost)
    try:
        candidate.__init__(workspace, descriptor_path, resident)
    except BaseException:
        if getattr(candidate, "timer", None):
            candidate.user.KillTimer(None, candidate.timer)
        if hasattr(candidate, "server"):
            candidate.server.stop()
        if hasattr(candidate, "panel"):
            candidate.panel.close()
        _retired.append(candidate)
        raise
    state_dir = getattr(candidate, "state_dir", None)
    mode = (state_dir and remembered_mode(state_dir)) or "auto"
    candidate.mode = mode
    candidate._remember_mode(mode)
    candidate.dev = dev
    candidate.driver = driver or ("journal" if resident else "timer")
    if dev:
        # Development launcher only: lets development agents inspect NXOpen
        # APIs inside NX. Never registered by the production launchers.
        candidate.services.dev_tools["nx_dev_python"] = candidate._dev_python
    _host = candidate
    return _host.status()

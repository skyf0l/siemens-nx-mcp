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

Panel modes (the radio buttons on the NX MCP panel):

- ``auto``: the engineer works in NX. Each agent call takes NX for itself only
  (NX's main window is disabled, no ``UI.LockAccess``), runs and gives NX back.
  Reads never wait; other calls wait while the engineer is busy in an NX
  command (``engineer_busy``), at most BUSY_WAIT_SECONDS. Long steps ask first.
- ``agent`` ("Agent drives"): NX input stays locked for the agent between calls.
- ``ask`` ("Ask me first"): every call waits for the engineer's answer.

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

from nx_mcp.core.errors import NXToolError

_host = None
_retired = []

# Ask me first: agent requests wait in the queue for an answer from the panel.
HOLD_SECONDS = 60
# Auto: a step waits at most this long while the engineer is busy in NX. The
# bridge (QUEUE_TIMEOUT) waits longer, so this answer always arrives first.
BUSY_WAIT_SECONDS = 120
PANEL_ROWS = 8  # Panel body lines: running/idle line or request card, then history.
NOTICE_SECONDS = 30  # A notice replaces the idle line this long.

MODES = ("auto", "agent", "ask")
MODE_LABELS = {"auto": "Auto", "agent": "Agent drives", "ask": "Ask me first"}
# How much control a mode gives the agent. The agent may lower it itself; only
# the engineer can raise it (a request to raise it waits for the panel's answer).
_RANK = {"ask": 0, "auto": 1, "agent": 2}
LEGACY_MODES = {"manual": "ask"}  # nx_set_control(mode="manual") = hand NX back
CONTROL_TOOL = "nx_set_control"  # the panel-mode tool (domains/session/control.py)
STATUS_TOOL = "nx_get_status"  # the status read (domains/session/status.py)

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

# Steps that change only the view or write an image; grouped with read-only
# inspections in the panel history so model changes stay visible.
VIEW_ONLY = frozenset({"nx_fit_view", "nx_set_view", "nx_screenshot", "nx_render_view"})


def _read_only(method):
    """Catalogue tools whose effect is ``read``."""
    from nx_mcp.host.dispatch import is_read_only

    return is_read_only(method)


def _model_unchanged(method):
    """Read and view steps: grouped in the panel history (model unchanged)."""
    from nx_mcp.host.dispatch import effect_of

    return _read_only(method) or method in VIEW_ONLY or effect_of(method) in ("read", "view")


def _running_tail(method):
    """Catalogue tools of cost instant/light do not pause NX noticeably (arch. 3.3)."""
    from nx_mcp.host.dispatch import cost_of

    return " — running" if cost_of(method) in ("instant", "light") else " — running, NX may pause"


def _long(method):
    """Catalogue cost class "long": Auto asks the engineer before running it."""
    from nx_mcp.host.dispatch import cost_of

    return cost_of(method) == "long"


def runs_under_ui_lock(method):
    """Agent drives keeps NX's UI lock during these calls (no per-call unlock and
    relock): reads, and catalogue view/model steps that open no file and create no
    part. Everything else runs unlocked as before: FileNew inside LockAccess can
    strand NX's UI lock (seen on NX 2606), and part loads may need it too."""
    from nx_mcp.host.dispatch import catalogue_spec

    spec = catalogue_spec(method)
    if spec is None:
        return _read_only(method)
    if spec.effect == "read":
        return True
    return spec.effect in ("model", "view") and not spec.paths and not spec.new_part


def _span(seconds):
    return f"{seconds // 60} minutes" if seconds >= 120 else f"{seconds} s"


def control_target(params):
    """The mode an nx_set_control call asks for ("status" for a read)."""
    mode = params.get("mode", "status")
    return LEGACY_MODES.get(mode, mode)


def runs_while_engineer_has_control(pending):
    """Status reads and handing NX back never wait for the engineer."""
    if pending.method == STATUS_TOOL:
        return True
    if pending.method == CONTROL_TOOL:
        return control_target(pending.params) in ("status", "ask")
    return False


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
ID_MODES = {111: "auto", 112: "agent", 113: "ask"}
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
        self.proc = self.proc_type(self._message)
        cls = WNDCLASS()
        cls.lpfnWndProc = self.proc
        cls.hbrBackground = 6
        cls.lpszClassName = "NxMcpPanel" + uuid.uuid4().hex
        self._class = cls
        if not self.user.RegisterClassW(ctypes.byref(cls)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = self.user.CreateWindowExW(
            0x80,
            cls.lpszClassName,
            "NX MCP control",
            0x10C80000,
            40,
            60,
            680,
            321,
            self.host.main_hwnd,
            None,
            None,
            None,
        )
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        def control(cls_name, text, style, x, y, width, height, ident=None):
            return self.user.CreateWindowExW(
                0, cls_name, text, style, x, y, width, height, self.hwnd, ident, None, None
            )

        # Mode: (•) Auto ( ) Agent drives ( ) Ask me first. BS_RADIOBUTTON (not
        # auto): a click only asks for the mode; the host sets the check marks.
        mode_label = control("STATIC", "Mode:", 0x50000000, 12, 12, 48, 20)
        self.radios = {
            "auto": control("BUTTON", "Auto", 0x50010004, 66, 8, 72, 24, 111),
            "agent": control("BUTTON", "Agent drives", 0x50000004, 144, 8, 118, 24, 112),
            "ask": control("BUTTON", "Ask me first", 0x50000004, 268, 8, 124, 24, 113),
        }
        # One list, newest first: the running step (or idle line / request card) on
        # top, then recent activity. The title says the mode and what the agent does.
        # SS_LEFTNOWORDWRAP (0xC): an entry never wraps into the next one; a line too
        # long for a high-DPI font is clipped at the right edge instead.
        self.label = control(
            "STATIC", "Starting interactive bridge...", 0x5000000C, 12, 38, 650, 164
        )
        # Production only: the engineer allows the agent to change the work part
        # (a part the agent neither created nor opened). BS_CHECKBOX, hidden until
        # needed; the host owns the checked state (show_grant).
        self.grant = control("BUTTON", "", 0x40010002, 12, 206, 650, 22, ID_GRANT)
        # The request card's answers replace "Open log" while the agent waits for
        # one. Stop sits apart so it is not hit by mistake.
        self.run_once = control("BUTTON", "Run once", 0x40010000, 12, 234, 110, 28, ID_RUN_ONCE)
        self.run_auto = control(
            "BUTTON", "Run && switch to Auto", 0x40010000, 128, 234, 170, 28, ID_RUN_AUTO
        )
        self.decline = control("BUTTON", "Not now", 0x40010000, 304, 234, 110, 28, ID_DECLINE)
        self.open_log = control("BUTTON", "Open log", 0x50010000, 12, 234, 110, 28, ID_OPEN_LOG)
        stop = control("BUTTON", "Stop bridge", 0x50010000, 532, 234, 130, 28, ID_STOP)
        # The dialog font fits a full activity line; the default system font does not.
        try:
            gdi = ctypes.WinDLL("gdi32")
            gdi.GetStockObject.restype = ctypes.c_void_p
            font = gdi.GetStockObject(17)  # DEFAULT_GUI_FONT
            children = (
                mode_label,
                *self.radios.values(),
                self.label,
                self.grant,
                self.run_once,
                self.run_auto,
                self.decline,
                self.open_log,
                stop,
            )
            for child in children:
                self.user.SendMessageW(child, 0x30, font, 1)  # WM_SETFONT
        except Exception as exc:
            self.host.last_error = "Panel font: " + str(exc)

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
                elif ident == ID_STOP:
                    self.host.stop_requested = True
                return 0
            if msg == 0x10:
                self.host.stop_requested = True
                return 0
        except BaseException as exc:
            self.host.last_error = str(exc)
        return self.user.DefWindowProcW(hwnd, msg, wp, lp)

    def update(self, text, title=None, mode=None, card=None):
        """Body text, window title, the checked mode, and the request card's buttons:
        None (no card), "ask" (Run once, Run & switch to Auto, Not now) or "auto"
        (Run once, Not now)."""
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
            self.user.ShowWindow(self.run_auto, 5 if card == "ask" else 0)
            # "Not now" closes the gap when "Run & switch to Auto" is hidden.
            self.user.MoveWindow(self.decline, 304 if card == "ask" else 128, 234, 110, 28, True)
            self.user.ShowWindow(self.decline, 5 if card else 0)
            self.user.ShowWindow(self.open_log, 0 if card else 5)
        # Paint only this panel before native work. Never pump arbitrary NX
        # messages here: that would permit reentrant modeling calls.
        self.user.UpdateWindow(self.label)
        self.user.UpdateWindow(self.hwnd)

    def show_grant(self, grant):
        """Show the "Allow agent to change <part>" checkbox, or hide it (None)."""
        if grant == getattr(self, "_grant", None):
            return
        self._grant = grant
        if grant is None:
            self.user.ShowWindow(self.grant, 0)
            return
        self.user.SetWindowTextW(self.grant, f"Allow agent to change {grant['name'][:60]}")
        self.user.SendMessageW(self.grant, 0xF1, 1 if grant["granted"] else 0, 0)  # BM_SETCHECK
        self.user.ShowWindow(self.grant, 5)

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
    auto_start = False  # Read by the startup app on a dev reload; the mode file wins.
    requested_mode = None
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
    handover_error = None
    open_log_requested = False
    grant_toggle_requested = False
    current_summary = None
    current_intent = None
    current_started_at = None
    idle_since = None
    recap = None
    window_disabled = False
    owns_lock = False
    _activity = None
    _light = False  # A call runs with NX taken for that call only (Auto, approved).
    _control_approved = False  # The engineer approved an nx_set_control call.
    _engineer_had_nx = True  # The engineer could edit since the agent's last call.
    _has_fingerprint = False
    _fingerprint = None
    _fast_accept = False
    _rewake = False
    _uf = None

    def __init__(self, workspace, descriptor_path, resident=False):
        import secrets

        import NXOpen

        from nx_mcp import nx_bridge
        from nx_mcp.bridge import (
            QUEUE_TIMEOUT,
            BridgeDescriptor,
            BridgeServer,
            MainThreadDispatcher,
        )
        from nx_mcp.host.services import NXServices
        from nx_mcp.workspace import Workspace

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
        self.owns_lock = False
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
        default_workspace = Path(os.environ.get("NX_MCP_WORKSPACE", r"D:\CAD\NX_MCP_WORKSPACE"))
        if self.root.resolve() == default_workspace.resolve() and batch_descriptor.exists():
            raise RuntimeError(
                "Stop the batch bridge before attaching the interactive host to this workspace"
            )
        self.services = NXServices(
            self.session,
            NXOpen,
            nx_bridge.detect_nx_version(self.session),
            Workspace(self.root),
        )
        self.services.host = self  # nx_set_control and nx_get_status reach the panel
        token = secrets.token_hex(32)
        progress = threading.Event()  # Set by bridge workers: a call queued or answered.
        self.dispatcher = MainThreadDispatcher(
            self.execute, queue_timeout=QUEUE_TIMEOUT, progress=progress
        )
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
        if method == CONTROL_TOOL and control_target(params) == "status" and snapshot:
            from nx_mcp.domains.session.control import envelope_from_snapshot

            return envelope_from_snapshot(snapshot, params)
        if method == STATUS_TOOL and snapshot and snapshot.get("running_method"):
            from nx_mcp.domains.session import status

            services = self.services
            return status.envelope_from_snapshot(
                snapshot,
                params,
                root=services.workspace.root,
                build=getattr(services, "build", None),
                store=getattr(services, "store", None),
            )
        return self.dispatcher.call(method, params)

    # -- panel -----------------------------------------------------------------

    def publish(self):
        state = self._refresh_snapshot()
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
        return "\n".join(lines + self.history_lines(PANEL_ROWS - len(lines)))

    def _summary_of(self, pending):
        if pending.summary is None:
            pending.summary = self._summarize(pending.method, pending.params)
        return pending.summary

    def panel_text(self):
        from nx_mcp.activity import LINE_WIDTH, _clip

        if self.running_method:
            # The panel cannot repaint while NX runs this step, so show a start
            # time rather than a counter that would appear frozen.
            started = time.strftime("%H:%M:%S", time.localtime(self.current_started_at))
            head, tail = f"▶  {started}  ", _running_tail(self.running_method)
            summary = self.current_summary or self.running_method
            lines = [head + _clip(summary, LINE_WIDTH - len(head) - len(tail)) + tail]
            if self.current_intent:
                indent = " " * (len(head) + 2)
                lines.append(indent + _clip(f"“{self.current_intent}”", LINE_WIDTH - len(indent)))
            return self._with_history(lines)
        if self.waiting_count:
            more = f"  (+{self.waiting_count - 1} more)" if self.waiting_count > 1 else ""
            lines = [f"▶ Agent wants to: {self.waiting_summary}{more}"]
            if self.waiting_intent:
                lines.append(f"   “{self.waiting_intent}”  (agent's note)")
            left = max(0, int(HOLD_SECONDS - (time.monotonic() - self.waiting_since)))
            why = "This step can take long. " if self.mode == "auto" else ""
            lines.append(f"{why}Answer within {left} s. Finish your current NX command first.")
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
        first = self.handover_error or self._notice_line()
        if self.mode == "agent":
            if not self.owns_lock:
                idle = "⏸  Waiting until you finish your command, then NX is locked for the agent"
            elif self.idle_since:
                since = time.strftime("%H:%M", time.localtime(self.idle_since))
                idle = f"·  Idle since {since} — waiting for the agent's next request"
            else:
                idle = "·  Waiting for the agent's first request"
            return self._with_history([first or idle])
        if self.mode == "auto":
            idle = "·  Auto — you work in NX, agent steps run between your commands"
        else:
            idle = "·  Ask me first — agent steps wait for your OK here"
        lines = [first or idle]
        if self.recap:
            lines.append(self.recap)
        return self._with_history(lines)

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
        card = None
        if self.waiting_count and not self.requested_mode:
            card = "ask" if self.mode == "ask" else "auto"
        return self.panel_text(), title, self.requested_mode or self.mode, card

    def status(self):
        waiting_command = 1 if self.busy_wait is not None else 0
        if self.running_method:
            activity = "running"
        elif self.waiting_count:
            activity = "waiting_for_answer"
        elif waiting_command:
            activity = "waiting_for_engineer_command"
        elif self.mode == "agent":
            activity = "reserved_idle" if self.owns_lock else "reserving"
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
            "operation_elapsed_seconds": (
                time.monotonic() - self.operation_started
                if self.operation_started is not None
                else None
            ),
            "last_duration_seconds": self.last_duration_seconds,
            "sampled_at": time.time(),
            "snapshot_only": False,
            "pause_semantics": "between operations; does not interrupt a native call",
            "reserved_for_agent": bool(self.mode == "agent" and self.owns_lock),
            "ui_locked_by_bridge": self.owns_lock,
            "actual_ui_lock": self.ui.AskLockStatus() == self.nx.UI.Status.Lock,
            "native_lock_value": str(self.ui.AskLockStatus()),
            "can_open_part": self.ui.CanOpenPart(),
            "last_unlock": getattr(self, "last_unlock", None),
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
            "uptime_seconds": time.time() - self.started,
            "request_wake": "fd_accept" if self._fast_accept else "timer",
            "scheduler": {
                "journal": "resident journal loop; one queued call per tick",
                "startup": "NX startup application timer on the UI thread; one queued call per tick",
            }.get(self.driver, "Win32 UI-thread timer; one queued call per tick"),
            "driver": self.driver,
        }

    # -- control ---------------------------------------------------------------

    def control(self, mode="status", *, by_engineer=False):
        """Report or switch the panel mode. Lowering the agent's control never fails:
        NX is released before any bookkeeping."""
        if threading.get_ident() != self.thread:
            raise RuntimeError("UI control called off NX thread")
        mode = LEGACY_MODES.get(mode, mode)
        if mode != "status":
            if mode not in MODES:
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "mode must be status, auto, agent or ask (manual = ask)"
                )
            previous = self.mode
            self.handover_error = None
            if previous == "agent" and mode != "agent":
                # Release first: the engineer gets NX back even if the bookkeeping
                # below fails. Nothing on this path raises.
                self._release_to_engineer()
                self._engineer_had_nx = True
                self._end_agent_session()
            self.mode = mode
            if mode == "agent" and previous != "agent":
                self.activity.start_session()
                self.idle_since = time.time()
                self.recap = None
                self.notice = None
            if mode == "agent":
                self._reserve_if_free()  # Pending while the engineer is busy in NX.
            if by_engineer:
                self._remember_mode(mode)
        try:
            return self.status()
        except Exception as exc:
            self._record_release_error("Status after mode change", exc)
            return {"interactive": True, "mode": self.mode, "last_error": self.last_error}

    def agent_control(self, mode):
        """nx_set_control from the agent: it may lower its control, never raise it
        unless the engineer approved the request on the panel."""
        target = LEGACY_MODES.get(mode, mode)
        if (
            target in _RANK
            and _RANK[target] > _RANK.get(self.mode, 0)
            and not self._control_approved
        ):
            raise NXToolError(
                "NX_UI_PAUSED",
                "Only the engineer can give the agent more control of NX. Ask them in chat "
                "to pick the mode on the NX MCP panel.",
                details={"mutation_outcome": "not_started"},
            )
        return self.control(target)

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

    def _reserve_if_free(self):
        """Agent drives: lock NX input for the agent unless the engineer is busy."""
        if self.owns_lock and self.window_disabled:
            return True
        if not self.owns_lock and self.engineer_busy():
            return False
        try:
            if not self.owns_lock:
                self.ui.LockAccess()
                self.owns_lock = True
            if not self.window_disabled:
                self.user.EnableWindow(self.main_hwnd, False)
                self.window_disabled = True
        except Exception as exc:
            self.last_error = "Cannot lock NX for the agent: " + str(exc)
            self._release_to_engineer()
            return False
        return True

    def _release_to_engineer(self):
        """Re-enable NX's window and drop the bridge's UI lock; never raises."""
        if self.window_disabled:
            try:
                self.user.EnableWindow(self.main_hwnd, True)
                self.window_disabled = False
            except Exception as exc:
                self._record_release_error("Cannot re-enable the NX window", exc)
        if self.owns_lock:
            before = None
            try:
                before = str(self.ui.AskLockStatus())
            except Exception as exc:
                self._record_release_error("Cannot read the NX UI lock", exc)
            try:
                self.ui.UnlockAccess()
                self.owns_lock = False
            except Exception as exc:
                self._record_release_error("Cannot unlock NX", exc)
            try:
                self.last_unlock = {
                    "before": before,
                    "after": str(self.ui.AskLockStatus()),
                    "can_open_part": self.ui.CanOpenPart(),
                }
            except Exception as exc:
                self._record_release_error("Cannot read the NX UI lock", exc)

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

    def _end_agent_session(self):
        """After Agent drives: clear highlights and recap; errors are recorded, never raised."""
        if hasattr(self.services, "clear_highlights"):
            try:
                self.services.clear_highlights()
            except Exception as exc:
                self._record_release_error("Cannot clear highlights", exc)
        try:
            self.recap = self.activity.session_recap()
        except Exception as exc:
            self._record_release_error("Activity recap", exc)

    def _lose_reservation(self, reason):
        """A host fault that cannot keep NX locked gives NX back and says so."""
        self._release_to_engineer()
        if self.mode == "agent":
            self.mode = "ask"
            self._engineer_had_nx = True
            self._end_agent_session()
            self._set_notice("NX MCP gave NX back to you (Ask me first): " + reason)

    # -- engineer activity -----------------------------------------------------

    def busy_signals(self):
        """What says the engineer is in the middle of something in NX (each ~µs)."""
        signals = {"simulated": bool(self.simulated_busy)}

        def probe(name, read):
            try:
                signals[name] = bool(read())
            except Exception:
                signals[name] = False

        if not self.owns_lock:  # Our own lock says nothing about the engineer.
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
        if now is not None and now == self._fingerprint:
            return False
        self._invalidate_references()
        try:
            self.activity.engineer_change()
        except Exception as exc:
            self.last_error = "Activity history: " + str(exc)
        return True

    def _invalidate_references(self):
        """Manual edits have no bridge receipt: a later agent rollback must not undo
        them, and references must not resolve across them. Never raises."""
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
        try:
            self.services.engineer_changed()  # undo history, checkpoints, review epoch
        except Exception as exc:
            self._record_release_error("Cannot clear rollback history", exc)

    # -- execution -------------------------------------------------------------

    def execute(self, method, params):
        from nx_mcp.host import dispatch

        if threading.get_ident() != self.thread:
            raise RuntimeError("NX request called off registering UI thread")
        if method in (CONTROL_TOOL, STATUS_TOOL):  # never waits for the engineer
            return dispatch.execute(self.services, method, params)
        reserved = self.mode == "agent" and self.owns_lock
        if not (reserved or self._light):
            raise NXToolError(
                "NX_UI_PAUSED",
                "The engineer has NX and has not approved this step. Ask them to answer "
                "the request on the NX MCP panel or to pick Auto.",
                details={"mutation_outcome": "not_started"},
            )
        if self.driver == "journal" and not _read_only(method) and not self._nx_application_ready():
            # Seen on NX 2406: the first part creation in a fresh session must
            # start NX's Modeling application, which waits for NX's own main
            # loop - held by this host - and freezes NX for good. Refuse instead.
            raise NXToolError(
                "NX_NO_PART_OPEN",
                "NX has no part open yet. In NX, create or open any part once "
                "(File > New or File > Open), then retry.",
                details={"mutation_outcome": "not_started"},
            )
        self._check_engineer_change()
        if reserved and not runs_under_ui_lock(method):
            # FileNew must not run inside LockAccess: NX v2606 can strand its
            # internal UI lock. The disabled main window reserves user input while
            # native operations execute on this same UI thread.
            self.ui.UnlockAccess()
        self.last_method = method
        self.running_method = method
        self.operation_started = time.monotonic()
        # Summarize before running: a delete removes the names it would show.
        self.current_summary = self._summarize(method, params)
        self.current_intent = params.get("intent")
        self.current_operation_id = params.get("operation_id")
        self.current_started_at = time.time()
        self.last_error = None
        result = error = None
        try:
            try:
                self.publish()
            except Exception as exc:
                self.last_error = "UI status publication failed: " + str(exc)
            result = dispatch.execute(self.services, method, params)
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
        except BaseException as exc:
            self.last_error = str(exc)
            error = exc
            raise
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
                )
            except Exception as exc:
                self.last_error = "Activity history: " + str(exc)
            self.running_method = None
            self.operation_started = None
            self.current_summary = self.current_intent = self.current_started_at = None
            self.idle_since = time.time()
            self._remember_model()
            if reserved:
                # Agent drives: NX stays locked between calls. Relock after an
                # unlocked call, or a call that dropped the lock itself.
                try:
                    if self.ui.AskLockStatus() != self.nx.UI.Status.Lock:
                        self.ui.LockAccess()
                except Exception as exc:
                    self.last_error = "Cannot restore agent UI reservation: " + str(exc)
                    self._lose_reservation("NX could not be locked again after a step.")
            try:
                self.publish()
            except Exception as exc:
                # A failed diagnostic write must not turn a committed operation
                # into a failure response that invites an unsafe retry.
                self.last_error = "UI status publication failed: " + str(exc)

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
            before = (self.mode, self.owns_lock, self.waiting_count)
            if self.requested_mode:
                mode, self.requested_mode = self.requested_mode, None
                try:
                    self.control(mode, by_engineer=True)
                except NXToolError as exc:
                    self.last_error = self.handover_error = str(exc)
            if self.open_log_requested:
                self.open_log_requested = False
                self._open_log()
            if self.grant_toggle_requested:
                self.grant_toggle_requested = False
                self._toggle_grant()
            self._apply_answer()
            if self.mode == "agent" and not self.owns_lock:
                self._reserve_if_free()
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
            if (
                ran
                or self.ticks % 10 == 0
                or before != (self.mode, self.owns_lock, self.waiting_count)
            ):
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
                self._lose_reservation("NX MCP hit an internal error: " + str(exc)[:80])
        finally:
            self.busy = False
            if ran or self._rewake:
                # Another call may be queued: tick again as soon as NX dispatches.
                self._rewake = False
                self._wake()

    # -- scheduling ------------------------------------------------------------

    def _never_waits(self, pending):
        """Status reads and mode changes that do not give the agent more control."""
        if pending.method == STATUS_TOOL:
            return True
        if pending.method == CONTROL_TOOL:
            target = control_target(pending.params)
            return target not in _RANK or _RANK[target] <= _RANK.get(self.mode, 0)
        return False

    def _needs_answer(self, pending):
        """Waits for the engineer's answer on the request card."""
        if pending.approved:
            return False
        if pending.method == CONTROL_TOOL:
            return True  # Only control raises reach here (_never_waits).
        if self.mode == "ask":
            return True
        return self.mode == "auto" and _long(pending.method)

    def _schedule(self):
        """Run at most one agent call; True when one ran."""
        if self.dispatcher.drain_where(self._never_waits):
            return True
        held = [p for p in self.dispatcher.waiting() if not self._never_waits(p)]
        if not held:
            self._clear_waiting()
            self.busy_wait = None
            return False
        now = time.monotonic()
        for pending in held:
            if pending.seen_at is None:
                pending.seen_at = now
        if self.mode == "agent":
            self._clear_waiting()
            if not self._reserve_if_free():
                return self._wait_for_engineer(held)
            self.busy_wait = None
            first = held[0]
            return bool(self.dispatcher.drain_where(lambda p: p is first))
        card = [p for p in held if self._needs_answer(p)]
        if card:
            self._show_card(card)  # May expire the card: those calls are answered.
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
        first = held[0]
        if self._needs_answer(first) or first.complete.is_set():
            self.busy_wait = None
            return False  # Calls run in order: the rest waits behind the card.
        if first.method == CONTROL_TOOL:
            return self._run_approved_control(first)
        if not _read_only(first.method) and self.engineer_busy():
            return self._wait_for_engineer([first])
        self.busy_wait = None
        return self._run_light(first)

    def _unasked_read(self, pending):
        return (
            pending.method != CONTROL_TOOL
            and not pending.complete.is_set()
            and _read_only(pending.method)
            and not self._needs_answer(pending)
        )

    def _run_light(self, pending, take=True):
        """Auto and approved steps: take NX for this call only, then give it back."""
        took = False
        try:
            took = take and self._take_light()
            self._light = True
            return bool(self.dispatcher.drain_where(lambda p: p is pending))
        finally:
            self._light = False
            if took:
                self._release_to_engineer()
            self._engineer_had_nx = True

    def _run_approved_control(self, pending):
        try:
            self._control_approved = True
            return bool(self.dispatcher.drain_where(lambda p: p is pending))
        finally:
            self._control_approved = False

    def _wait_for_engineer(self, pending_calls):
        """The engineer is busy in NX: wait, at most BUSY_WAIT_SECONDS per call."""
        now = time.monotonic()
        expired = {id(p) for p in pending_calls if now - p.seen_at >= BUSY_WAIT_SECONDS}
        if expired:
            span = _span(BUSY_WAIT_SECONDS)
            self.dispatcher.reject_where(
                lambda p: id(p) in expired,
                NXToolError(
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
        if self.waiting_since is None:
            self.waiting_since = time.monotonic()
            self.notice = None
            self._flash_nx()
        self.card = tuple(waiting)
        if time.monotonic() - self.waiting_since >= HOLD_SECONDS:
            self._reject_card(
                NXToolError(
                    "NX_UI_PAUSED",
                    f"The engineer did not answer within {HOLD_SECONDS} s. Ask them in chat, "
                    "or ask them to answer the request on the NX MCP panel.",
                    retryable=True,
                ),
                f"The agent's request expired after {HOLD_SECONDS} s without an answer.",
            )
            return
        first = waiting[0]
        self.waiting_count = len(waiting)
        self.waiting_summary = self._summary_of(first)
        self.waiting_intent = first.params.get("intent")

    def _reject_card(self, error, notice):
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
                NXToolError(
                    "NX_UI_DECLINED",
                    "The engineer chose 'Not now' in the NX MCP control window. "
                    "Ask in chat before requesting this again.",
                ),
                "You declined the agent's request.",
            )
            return
        for pending in self.card:
            pending.approved = True
        self._clear_waiting()
        if answer == "run_auto":
            self.control("auto", by_engineer=True)

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
        `result` to return a value; print() output is returned as stdout.
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
        with contextlib.redirect_stdout(output):
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

        try:
            self.control("ask")
        except Exception as exc:
            self._record_release_error("Handover on stop", exc)
            self._release_to_engineer()
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


def start(
    workspace, descriptor_path, resident=False, *, agent_control=None, dev=False, driver=None
):
    """Start the host. The panel mode is the one last chosen in this NX process
    (kept across development reloads), else Agent drives when agent_control is
    True (default: in the development loop) and Auto otherwise. dev marks the
    panel of the development loop. driver names what ticks the host: "timer"
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
    if agent_control is None:
        agent_control = dev
    state_dir = getattr(candidate, "state_dir", None)
    mode = (state_dir and remembered_mode(state_dir)) or ("agent" if agent_control else "auto")
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

"""Windows NX UI-thread host. No background thread calls NXOpen.

Timer mode (NX 2606): the journal returns after installing a Win32 timer. NX's
existing message pump invokes TIMERPROC on the registering thread; the pinned
callback drains one call.

Resident mode (NX 2406 and other releases that tear down a journal's
interpreter when it returns): the journal never returns while the host is up.
It pumps the thread's message queue itself and ticks the host from its own
interpreter, so lazy imports and NXOpen namespaces resolve where they were set
up. See run_resident().
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import uuid
from pathlib import Path

from nx_mcp.runtime import NXToolError
from nx_mcp.winpump import pump_messages

_host = None
_retired = []

# While the engineer has control, agent requests wait in the queue for an
# answer from the panel. Keep this well under the 120 s bridge timeout so an
# approved request still has time to run.
HOLD_SECONDS = 60

# Steps that change only the view or write an image; grouped with read-only
# inspections in the panel history so model changes stay visible.
VIEW_ONLY = frozenset({"nx_fit_view", "nx_set_view", "nx_screenshot", "nx_render_view"})


def runs_while_engineer_has_control(pending):
    """Status reads and handing control back never need the engineer's approval."""
    if pending.method == "nx_status":
        return True
    if pending.method == "nx_ui_control":
        return pending.params.get("mode", "status") != "agent"
    return False


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
        self.user.UpdateWindow.argtypes = [w.HWND]
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
            560,
            245,
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

        # Status (who has control, current or waiting step) above recent activity.
        self.label = control("STATIC", "Starting interactive bridge...", 0x50000000, 12, 8, 530, 72)
        self.history = control("STATIC", "", 0x50000000, 12, 84, 530, 72)
        # One toggle hands control over in either direction; its caption names
        # the only available action. "Not now" replaces "Open log" only while the
        # agent is waiting for control. Stop sits apart so it is not hit by mistake.
        self.toggle = control("BUTTON", "Give control to agent", 0x50010000, 12, 164, 250, 28, 101)
        self.decline = control("BUTTON", "Not now", 0x40010000, 270, 164, 110, 28, 104)
        self.open_log = control("BUTTON", "Open log", 0x50010000, 270, 164, 110, 28, 105)
        stop = control("BUTTON", "Stop bridge", 0x50010000, 412, 164, 130, 28, 103)
        # The dialog font fits a full activity line; the default system font does not.
        try:
            gdi = ctypes.WinDLL("gdi32")
            gdi.GetStockObject.restype = ctypes.c_void_p
            font = gdi.GetStockObject(17)  # DEFAULT_GUI_FONT
            self.user.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
            for child in (self.label, self.history, self.toggle, self.decline, self.open_log, stop):
                self.user.SendMessageW(child, 0x30, font, 1)  # WM_SETFONT
        except Exception as exc:
            self.host.last_error = "Panel font: " + str(exc)

    def _message(self, hwnd, msg, wp, lp):
        try:
            if msg == 0x111:
                ident = int(wp) & 0xFFFF
                if ident == 101 and self.host.requested_mode is None:
                    self.host.requested_mode = "manual" if self.host.mode == "agent" else "agent"
                elif ident == 104:
                    self.host.decline_requested = True
                elif ident == 105:
                    self.host.open_log_requested = True
                elif ident == 103:
                    self.host.stop_requested = True
                return 0
            if msg == 0x10:
                self.host.stop_requested = True
                return 0
        except BaseException as exc:
            self.host.last_error = str(exc)
        return self.user.DefWindowProcW(hwnd, msg, wp, lp)

    def update(
        self,
        text,
        title=None,
        button=None,
        button_enabled=True,
        decline_visible=False,
        history=None,
    ):
        state = (text, title, button, button_enabled, decline_visible, history)
        if state == getattr(self, "_text", None):
            return
        self._text = state
        self.user.SetWindowTextW(self.label, text)
        if history is not None:
            self.user.SetWindowTextW(self.history, history)
        if title is not None:
            self.user.SetWindowTextW(self.hwnd, title)
        if button is not None:
            self.user.SetWindowTextW(self.toggle, button)
            self.user.EnableWindow(self.toggle, button_enabled)
            self.user.ShowWindow(self.decline, 5 if decline_visible else 0)
            self.user.ShowWindow(self.open_log, 0 if decline_visible else 5)
        # Paint only this panel before native work. Never pump arbitrary NX
        # messages here: that would permit reentrant modeling calls.
        self.user.UpdateWindow(self.label)
        self.user.UpdateWindow(self.hwnd)

    def close(self):
        self.user.DestroyWindow(self.hwnd)
        self.user.UnregisterClassW(self._class.lpszClassName, None)


class InteractiveHost:
    dev = False
    hold_auto_start = False
    resident = False
    timer = None
    waiting_since = None
    waiting_count = 0
    waiting_summary = None
    waiting_intent = None
    decline_requested = False
    notice = None
    open_log_requested = False
    current_summary = None
    current_intent = None
    current_started_at = None
    idle_since = None
    recap = None
    _activity = None

    def __init__(self, workspace, descriptor_path, resident=False):
        import secrets

        import NXOpen

        from nx_mcp import nx_bridge
        from nx_mcp.bridge import BridgeDescriptor, BridgeServer, MainThreadDispatcher
        from nx_mcp.hardened import HardenedExecutor
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
        self.mode = "manual"
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
        self.executor = HardenedExecutor(
            self.session,
            NXOpen,
            nx_bridge._detect_nx_version(self.session),
            Workspace(self.root),
        )
        self.executor._handlers["nx_ui_control"] = self.control
        token = secrets.token_hex(32)
        self.dispatcher = MainThreadDispatcher(self.execute)
        self.server = BridgeServer(
            self.dispatch,
            token=token,
            concurrent_requests=True,
            result_directory=Path(self.root) / ".nx-mcp" / "bridge-results",
        )
        self.descriptor_path = Path(descriptor_path)
        self.server.start()
        self.descriptor = BridgeDescriptor.create(
            self.server.port, self.executor.nx_version, token=token
        )
        self.main_hwnd = nx_main_window()
        self.window_disabled = False
        self.panel = ControlPanel(self)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.EnableWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.user.IsWindowEnabled.argtypes = [ctypes.c_void_p]
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
        # A resident journal starts in manual mode so NX stays usable after
        # Play; the sidecar or panel switches to agent mode when needed.
        self.auto_start = not resident

    def dispatch(self, method, params):
        # Bridge-worker health reads use an immutable UI-thread snapshot only.
        # They must not wait behind the native operation they are diagnosing.
        snapshot = getattr(self, "_snapshot", None)
        if method == "nx_ui_control" and params.get("mode", "status") == "status" and snapshot:
            result = dict(snapshot)
            result["snapshot_age_seconds"] = max(
                0, time.monotonic() - result.pop("_sample_monotonic")
            )
            result["snapshot_only"] = True
            started = result.pop("_operation_monotonic", None)
            result["operation_elapsed_seconds"] = (
                max(0, time.monotonic() - started) if started is not None else None
            )
            return result
        return self.dispatcher.call(method, params)

    def publish(self):
        state = self.status()
        self._snapshot = {
            **state,
            "_sample_monotonic": time.monotonic(),
            "_operation_monotonic": self.operation_started,
        }
        self.panel.update(*self.panel_state(), history=self.history_text())
        temp = self.state_dir / "ui-state.tmp"
        temp.write_text(json.dumps(state))
        temp.replace(self.state_dir / "ui-state.json")

    @property
    def activity(self):
        if self._activity is None:
            from nx_mcp.activity import Activity

            state_dir = getattr(self, "state_dir", None)
            self._activity = Activity(state_dir / "activity.log" if state_dir else None)
        return self._activity

    def history_text(self):
        lines = self.activity.recent(4)
        return "Recent agent activity\n" + ("\n".join(lines) if lines else "Nothing yet.")

    def panel_text(self):
        if self.running_method:
            # The panel cannot repaint while NX runs this step, so show a start
            # time rather than a counter that would appear frozen.
            started = time.strftime("%H:%M:%S", time.localtime(self.current_started_at))
            lines = [
                "● Agent working",
                f"▶ {self.current_summary or self.running_method}",
            ]
            if self.current_intent:
                lines.append(f"   “{self.current_intent}”  (agent's note)")
            lines.append(f"Started {started} · NX may freeze until this step finishes.")
            return "\n".join(lines)
        handover_error = getattr(self, "handover_error", None)
        if self.mode == "manual" and self.waiting_count:
            more = f"  (+{self.waiting_count - 1} more)" if self.waiting_count > 1 else ""
            lines = ["● You have control", f"▶ Agent wants to: {self.waiting_summary}{more}"]
            if self.waiting_intent:
                lines.append(f"   “{self.waiting_intent}”  (agent's note)")
            left = max(0, int(HOLD_SECONDS - (time.monotonic() - self.waiting_since)))
            lines.append(
                handover_error or f"Answer within {left} s. Finish your current NX command first."
            )
            return "\n".join(lines)
        if self.mode == "manual":
            return "● You have control\n" + (
                handover_error
                or self.notice
                or self.recap
                or "Agent requests wait until you give control to the agent."
            )
        idle = (
            " since " + time.strftime("%H:%M", time.localtime(self.idle_since))
            if self.idle_since
            else ""
        )
        return (
            "● Agent has control — NX input is locked\n"
            f"Idle{idle} · waiting for the agent's next request.\n"
            "Take control back at any time to work in NX."
        )

    def panel_state(self):
        """Label, window title, toggle caption, toggle enabled, and "Not now" visibility."""
        waiting = self.mode == "manual" and bool(self.waiting_count)
        if waiting:
            owner = "Agent is waiting"
        elif self.running_method:
            owner = "Agent working"
        else:
            owner = "Agent has control" if self.mode == "agent" else "You have control"
        if getattr(self, "requested_mode", None):
            button, enabled = "Handing over…", False
        elif self.running_method:
            button, enabled = "Take control back after this step", True
        elif self.mode == "agent":
            button, enabled = "Take control back", True
        else:
            button, enabled = "Give control to agent", True
        decline = waiting and not getattr(self, "requested_mode", None)
        product = "NX MCP DEV" if self.dev else "NX MCP"
        return self.panel_text(), f"{product} — {owner}", button, enabled, decline

    def status(self):
        return {
            "interactive": True,
            "pid": os.getpid(),
            "mode": self.mode,
            "activity": "running"
            if self.running_method
            else ("reserved_idle" if self.mode == "agent" else "manual"),
            "running_method": self.running_method,
            "operation_elapsed_seconds": (
                time.monotonic() - self.operation_started
                if self.operation_started is not None
                else None
            ),
            "last_duration_seconds": self.last_duration_seconds,
            "sampled_at": time.time(),
            "snapshot_only": False,
            "pause_semantics": "between operations; does not interrupt a native call",
            "ui_locked_by_bridge": self.owns_lock,
            "actual_ui_lock": self.ui.AskLockStatus() == self.nx.UI.Status.Lock,
            "native_lock_value": str(self.ui.AskLockStatus()),
            "can_open_part": self.ui.CanOpenPart(),
            "last_unlock": getattr(self, "last_unlock", None),
            "nx_window_input_enabled": bool(self.user.IsWindowEnabled(self.main_hwnd)),
            "main_thread_id": self.native_thread,
            "callback_thread_id": ctypes.windll.kernel32.GetCurrentThreadId(),
            "ticks": self.ticks,
            "completed_operations": self.completed,
            "last_method": self.last_method,
            "last_error": self.last_error,
            "waiting_for_engineer": self.waiting_count,
            "waiting_seconds": (
                time.monotonic() - self.waiting_since if self.waiting_since is not None else None
            ),
            "uptime_seconds": time.time() - self.started,
            "scheduler": (
                "resident journal loop; one queued call per tick"
                if self.resident
                else "Win32 UI-thread timer; one queued call per tick"
            ),
        }

    def control(self, mode="status", *, by_engineer=False):
        if threading.get_ident() != self.thread:
            raise RuntimeError("UI control called off NX thread")
        if mode == "manual":
            if self.mode == "agent":
                if hasattr(self.executor, "_clear_highlights"):
                    self.executor._clear_highlights()
                # Manual edits have no bridge receipt. Do not let a later agent
                # rollback undo them, or resolve references across that boundary.
                for part in self.session.Parts:
                    self.executor.objects.invalidate_part(self.executor._part_id(part))
                self.executor._history.clear()
                self.executor._checkpoints.clear()
                self.executor._review_epoch = getattr(self.executor, "_review_epoch", 0) + 1
                self.recap = self.activity.session_recap()
            if self.owns_lock:
                before = str(self.ui.AskLockStatus())
                self.ui.UnlockAccess()
                self.last_unlock = {
                    "before": before,
                    "after": str(self.ui.AskLockStatus()),
                    "can_open_part": self.ui.CanOpenPart(),
                }
                self.owns_lock = False
            if self.window_disabled:
                self.user.EnableWindow(self.main_hwnd, True)
                self.window_disabled = False
            self.mode = "manual"
            # Only the engineer's own click is a lasting choice. After the agent
            # hands control back, or an internal error drops it, the wish for
            # agent control is kept but held: this host never acts on it, while
            # the development loop restores it on the next reload.
            self.auto_start = not by_engineer
            self.hold_auto_start = True
        elif mode == "agent":
            if not self.owns_lock:
                if self.ui.AskLockStatus() == self.nx.UI.Status.Lock or not self.ui.CanOpenPart():
                    raise NXToolError(
                        "NX_UI_BUSY",
                        "Close the open NX dialog, then give control to the agent again.",
                        details={"mutation_outcome": "not_started"},
                    )
                self.ui.LockAccess()
                self.owns_lock = True
            self.user.EnableWindow(self.main_hwnd, False)
            self.window_disabled = True
            self.auto_start = self.hold_auto_start = False
            if self.mode != "agent":
                self.activity.start_session()
                self.idle_since = time.time()
                self.recap = None
                self.notice = None
            self.mode = "agent"
            self.handover_error = None
        elif mode != "status":
            raise NXToolError("NX_INVALID_ARGUMENT", "mode must be status, manual or agent")
        return self.status()

    def execute(self, method, params):
        if threading.get_ident() != self.thread:
            raise RuntimeError("NX request called off registering UI thread")
        if method == "nx_ui_control":
            return self.control(params.get("mode", "status"))
        if method == "nx_status":
            return {**self.executor.execute(method, params), "ui": self.status()}
        if self.mode != "agent" or not self.owns_lock:
            raise NXToolError(
                "NX_UI_PAUSED",
                "The user has control of NX. Ask them to click 'Give control to agent' "
                "in the NX MCP control window.",
                details={"mutation_outcome": "not_started"},
            )
        from nx_mcp.hardened import READ_ONLY

        if method not in READ_ONLY and not self._nx_application_ready():
            # Seen on NX 2406: the first part creation in a fresh session must
            # start NX's Modeling application, which waits for NX's own main
            # loop - held by this host - and freezes NX for good. Refuse instead.
            raise NXToolError(
                "NX_NO_PART_OPEN",
                "NX has no part open yet. In NX, create or open any part once "
                "(File > New or File > Open), then retry.",
                details={"mutation_outcome": "not_started"},
            )
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
        self.current_started_at = time.time()
        self.last_error = None
        result = error = None
        try:
            try:
                self.publish()
            except Exception as exc:
                self.last_error = "UI status publication failed: " + str(exc)
            result = self.executor.execute(method, params)
            self.completed += 1
            part = self.session.Parts.Display
            sheet = getattr(getattr(part, "DrawingSheets", None), "CurrentDrawingSheet", None)
            if part and method not in READ_ONLY and sheet is None:
                try:
                    part.ModelingViews.WorkView.UpdateDisplay()
                except Exception as exc:
                    result.setdefault("warnings", []).append("View refresh: " + str(exc))
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
                    read_only=method in READ_ONLY or method in VIEW_ONLY,
                    result=result,
                    error=error,
                )
            except Exception as exc:
                self.last_error = "Activity history: " + str(exc)
            self.running_method = None
            self.operation_started = None
            self.current_summary = self.current_intent = self.current_started_at = None
            self.idle_since = time.time()
            # Restore the between-operation native lock after all NX work.
            try:
                if self.ui.AskLockStatus() != self.nx.UI.Status.Lock:
                    self.ui.LockAccess()
            except Exception as exc:
                self.last_error = "Cannot restore agent UI reservation: " + str(exc)
                self.control("manual")
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
        try:
            self.ticks += 1
            if self.stop_requested or self.stop_file.exists():
                self.stop()
                return
            if self.requested_mode:
                mode, self.requested_mode = self.requested_mode, None
                try:
                    self.control(mode, by_engineer=True)
                except NXToolError as exc:
                    self.last_error = self.handover_error = str(exc)
            if self.open_log_requested:
                self.open_log_requested = False
                self._open_log()
            if self.auto_start and not self.hold_auto_start:
                try:
                    self.control("agent")
                    self.auto_start = False
                except NXToolError:
                    pass
            if self.mode == "agent":
                self._clear_waiting()
                self.dispatcher.drain(timeout=0, limit=1)
            else:
                self._serve_while_engineer_has_control()
            self.panel.update(*self.panel_state(), history=self.history_text())
            if self.ticks % 10 == 0:
                self.publish()
        except BaseException as exc:
            self.last_error = str(exc)
            try:  # noqa: SIM105 - Last-resort native callback cleanup must not escape.
                self.control("manual")
            except BaseException:
                pass
        finally:
            self.busy = False

    def _serve_while_engineer_has_control(self):
        """Run status calls; hold everything else for the engineer's answer."""
        if self.dispatcher.drain_where(runs_while_engineer_has_control):
            return

        def held(pending):
            return not runs_while_engineer_has_control(pending)

        waiting = [p for p in self.dispatcher.waiting() if held(p)]
        if not waiting:
            self._clear_waiting()
            self.decline_requested = False
            return
        now = time.monotonic()
        if self.waiting_since is None:
            self.waiting_since = now
            self.notice = None
            self._flash_nx()
        if self.decline_requested:
            self.dispatcher.reject_where(
                held,
                NXToolError(
                    "NX_UI_DECLINED",
                    "The engineer chose 'Not now' in the NX MCP control window. "
                    "Ask in chat before requesting control again.",
                ),
            )
            self._clear_waiting(notice="You declined the agent's request.")
            return
        if now - self.waiting_since >= HOLD_SECONDS:
            self.dispatcher.reject_where(
                held,
                NXToolError(
                    "NX_UI_PAUSED",
                    f"The engineer did not give control within {HOLD_SECONDS} s. Ask them to "
                    "click 'Give control to agent' in the NX MCP control window.",
                    retryable=True,
                ),
            )
            self._clear_waiting(
                notice=f"The agent's request expired after {HOLD_SECONDS} s without an answer."
            )
            return
        first = waiting[0]
        if getattr(first, "summary", None) is None:
            first.summary = self._summarize(first.method, first.params)
        self.waiting_count = len(waiting)
        self.waiting_summary = first.summary
        self.waiting_intent = first.params.get("intent")

    def _clear_waiting(self, notice=None):
        self.waiting_since = None
        self.waiting_count = 0
        self.waiting_summary = None
        self.waiting_intent = None
        self.decline_requested = False
        if notice:
            self.notice = notice

    def _summarize(self, method, params):
        from nx_mcp.request_summary import humanize, relative_path, summarize

        try:
            units = self.executor._units()
        except Exception:
            units = None
        try:
            return summarize(
                method,
                params,
                name_of=self.executor.objects.display_name,
                path_of=relative_path(self.root),
                units=units,
            )
        except Exception:
            # A summary is a courtesy; it must never stop a request from waiting.
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

        Names: NXOpen, session, part (work part), executor, resolve(id). Set
        `result` to return a value; print() output is returned as stdout.
        """
        import contextlib
        import io

        namespace = {
            "NXOpen": self.nx,
            "session": self.session,
            "part": self.session.Parts.Work,
            "executor": self.executor,
            "resolve": lambda ref: self.executor._resolve(ref),
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
            self.notice = "No agent activity logged yet."
            return
        try:
            os.startfile(log)  # Opens in the engineer's default text editor.
        except OSError as exc:
            self.notice = f"Cannot open {log}: {exc}"

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

        self.control("manual")
        self.stopped = True
        if self.timer:
            self.user.KillTimer(None, self.timer)
        self.dispatcher.stop()
        self.server.stop()
        self.panel.close()
        try:
            if BridgeDescriptor.read(self.descriptor_path).token == self.descriptor.token:
                self.descriptor_path.unlink()
        except (ValueError, OSError):
            pass


def start(workspace, descriptor_path, resident=False, *, agent_control=None, dev=False):
    """Start the host. agent_control=True gives the agent control as soon as NX
    allows it (default: timer hosts do, resident hosts start with the engineer).
    dev marks the panel of the development loop."""
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
    if agent_control is not None:
        candidate.auto_start = agent_control
    candidate.dev = dev
    if dev:
        # Development launcher only: lets development agents inspect NXOpen
        # APIs inside NX. Never registered by the production launchers.
        candidate.executor._handlers["nx_dev_python"] = candidate._dev_python
    _host = candidate
    return _host.status()


def run_resident(workspace, descriptor_path, interval=0.02, pump=pump_messages):
    """Host the bridge from a journal that stays resident until Stop.

    Messages are pumped only between ticks, never during a native call, so no
    modeling request can re-enter while another is running.
    """
    missing = import_nxopen_namespaces()
    start(workspace, descriptor_path, resident=True)
    host = _host
    if missing:
        host.last_error = "NXOpen namespaces not importable: " + "; ".join(missing)
    while not host.stopped:
        pump()
        host.tick()
        time.sleep(interval)
    return host

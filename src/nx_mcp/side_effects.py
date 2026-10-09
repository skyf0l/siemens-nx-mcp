"""Observe whether a tool handler may already have caused a side effect.

Tools without an outer undo mark (files, views, session lifecycle) cannot roll
back, so a failure normally reports ``mutation_outcome: "unknown"``. Most such
failures are plain argument checks that run before anything happens. The kernel
wraps every handler in a :class:`SideEffectObserver` (``NXServices.observe``) on
top of the ``Tx`` touch tracking; the observer is "touched" (side effects
possible) as soon as the handler, on the NX thread:

* obtains an NX object from the object registry,
* calls any NXOpen function or method, including ``Session.GetSession`` and
  ``UFSession.GetUFSession`` (``sys.setprofile`` ``c_call`` events), or runs
  Python code in an NXOpen module,
* writes, renames, deletes or copies a file, starts a process, opens a socket,
  calls a foreign function through ctypes or edits the registry (audit hooks).

A failure while the observer is untouched provably happened before any side
effect and reports ``"not_started"``; anything else stays ``"unknown"``. The one
blind spot is a property assignment on an NX object cached in an earlier call
without any further NXOpen call, which handlers do not do before validating
their arguments.
"""

from __future__ import annotations

import os
import sys
import threading

_WRITE_FLAGS = (
    os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | getattr(os, "O_EXCL", 0)
)
_READ_ONLY_EVENTS = frozenset(
    {
        "os.listdir",
        "os.scandir",
        "os.walk",
        "os.fwalk",
        "os.getxattr",
        "os.listxattr",
        "socket.gethostname",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
        "socket.getservbyname",
        "socket.getservbyport",
    }
)
_SIDE_EFFECT_PREFIXES = (
    "os.",
    "shutil.",
    "subprocess.",
    "_posixsubprocess.",
    "ctypes.",
    "winreg.",
    "socket.",
    "_winapi.",
    "msvcrt.",
    "tempfile.",
    "webbrowser.",
    "sqlite3.",
    "mmap.",
    "http.client.",
    "urllib.",
    "ftplib.",
    "smtplib.",
    "imaplib.",
    "poplib.",
    "nntplib.",
    "telnetlib.",
)

_local = threading.local()


def _stack():
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = _local.stack = []
    return stack


def _current():
    stack = getattr(_local, "stack", None)
    return stack[-1] if stack else None


def touch():
    """Record that the running handler may have caused a side effect."""
    observer = _current()
    if observer is not None and not observer.touched:
        observer.touched = True
        observer._stop_profile()


def _is_nx(function):
    module = getattr(function, "__module__", None)
    if isinstance(module, str) and module.startswith("NXOpen"):
        return True
    owner = getattr(function, "__self__", None)
    if owner is None:
        owner = getattr(function, "__objclass__", None)
    if owner is None:
        return False
    cls = owner if isinstance(owner, type) else type(owner)
    module = getattr(cls, "__module__", None)
    return isinstance(module, str) and module.startswith("NXOpen")


def _profile(frame, event, arg):
    if event == "c_call":
        if _is_nx(arg):
            touch()
    elif event == "call":
        name = frame.f_globals.get("__name__")
        if isinstance(name, str) and name.startswith("NXOpen"):
            touch()


def _audit(event, args):
    stack = getattr(_local, "stack", None)
    if not stack or stack[-1].touched:
        return
    if event == "open":
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writes = (
            any(c in mode for c in "wax+")
            if isinstance(mode, str)
            else bool(isinstance(flags, int) and flags & _WRITE_FLAGS)
        )
        if writes:
            touch()
    elif event not in _READ_ONLY_EVENTS and event.startswith(_SIDE_EFFECT_PREFIXES):
        touch()


def _install_audit_hook():
    # Audit hooks cannot be removed; a development reload re-imports this module,
    # so one permanent hook forwards to whichever module version is current.
    sys._nx_mcp_side_effect_audit = _audit
    if not getattr(sys, "_nx_mcp_side_effect_hook", False):

        def forward(event, args):
            target = getattr(sys, "_nx_mcp_side_effect_audit", None)
            if target is not None:
                target(event, args)

        sys.addaudithook(forward)
        sys._nx_mcp_side_effect_hook = True


class SideEffectObserver:
    """Context for one handler call; ``touched`` means side effects are possible."""

    def __init__(self):
        self.touched = False
        self._profiling = False

    def start(self):
        _install_audit_hook()
        _stack().append(self)
        self._start_profile()
        return self

    def stop(self):
        self._stop_profile()
        stack = _stack()
        if stack and stack[-1] is self:
            stack.pop()
        # A nested tool call is itself possible side effect of the outer handler.
        touch()

    def _start_profile(self):
        if self._profiling or self.touched:
            return
        previous = sys.getprofile()
        if previous is not None and previous is not _profile:
            # Another profiler is active (a debugger): we cannot observe NXOpen
            # calls, so assume side effects are possible.
            self.touched = True
            return
        sys.setprofile(_profile)
        self._profiling = True

    def _stop_profile(self):
        if self._profiling:
            self._profiling = False
            outer = next((o for o in reversed(_stack()) if o is not self and o._profiling), None)
            sys.setprofile(_profile if outer is not None else None)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

"""No NX prompt during an agent call: update failures and the listing window.

Two NX behaviours open windows that wait for a person and block NX for everyone:

- An update failure with NX's default update-failure action (``NoOption``, the
  engineer's normal setting) opens the modal "Error During Update" dialog (an
  expression cycle, a feature parameter NX cannot build, a failing ``DoUpdate``).
  During a call the default action is ``Undo``: NX undoes the failed update itself
  and the call fails, which the kernel maps to its usual rollback and error. It is
  never an ``Accept`` action, which would keep a failed model.
- Output written to NX's listing window opens the "Information" window. During a
  call the listing window writes to a file instead; its text goes into the result.

Both settings are the engineer's: the previous ones are restored when the call ends,
whatever happens (``try/finally``). Every step tolerates a missing API (the fakes,
batch NX), so the guard never fails a call.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LISTING_LIMIT = 20000


@dataclass
class Calm:
    """What :func:`quiet` learned: the listing text the call wrote (``""`` if none)."""

    listing: str = ""
    failure_action: object = None  # the engineer's setting while the call ran


_active = 0  # nesting depth: only the outermost guard changes and restores NX
_lock = threading.Lock()


@contextlib.contextmanager
def quiet(session, nx, folder, calm: Calm | None = None):
    """Run a call with no NX update-failure dialog and no listing window.

    Re-entrant: a guard entered while another is active (a nested call, a status read
    from another thread) changes nothing, so it cannot restore the outer call's
    settings early."""
    global _active
    calm = calm if calm is not None else Calm()
    with _lock:
        outer = _active == 0
        _active += 1
    try:
        if not outer:
            yield calm
            return
        with contextlib.ExitStack() as stack:
            stack.enter_context(_update_failures(session, nx, calm))
            stack.enter_context(_listing(session, nx, Path(folder), calm))
            yield calm
    finally:
        with _lock:
            _active -= 1


@contextlib.contextmanager
def _update_failures(session, nx, calm: Calm):
    manager: Any = getattr(session, "UpdateManager", None)
    options: Any = getattr(nx, "UpdateFailureOption", None)
    previous = None
    with contextlib.suppress(Exception):
        previous = manager.GetDefaultUpdateFailureAction()
        manager.SetDefaultUpdateFailureAction(options.Undo)
    calm.failure_action = previous
    try:
        yield
    finally:
        if previous is not None:
            with contextlib.suppress(Exception):
                manager.SetDefaultUpdateFailureAction(previous)


@contextlib.contextmanager
def _listing(session, nx, folder: Path, calm: Calm):
    window: Any = getattr(session, "ListingWindow", None)
    devices: Any = getattr(nx, "ListingWindowDeviceType", None)
    path = folder / f"listing-{uuid.uuid4().hex[:12]}.txt"
    previous = was_open = None
    with contextlib.suppress(Exception):
        current, was_open = window.Device, bool(window.IsOpen)
        folder.mkdir(parents=True, exist_ok=True)
        window.SelectDevice(devices.File, str(path))
        previous = current
    try:
        yield
    finally:
        if previous is not None:
            with contextlib.suppress(Exception):
                if window.IsOpen and not was_open:
                    window.Close()  # opened by the call (on the file)
            with contextlib.suppress(Exception):
                if previous == devices.NotSet:
                    previous = devices.Window
                # Selecting the device again flushes the file (NX buffers file output).
                window.SelectDevice(previous, "")
            with contextlib.suppress(Exception):
                text = path.read_text(encoding="utf-8", errors="replace")
                calm.listing = text.lstrip("﻿")[-LISTING_LIMIT:]
            with contextlib.suppress(Exception):
                path.unlink(missing_ok=True)

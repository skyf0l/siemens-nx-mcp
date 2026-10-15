"""Tell a busy NX from a hung or stopped one without asking NX (sidecar side).

While NX's main thread runs one long native step it does not pump Windows messages,
so the interactive host cannot even accept a bridge connection: a status read sent
then would wait for the whole step and could only report the bridge "unavailable".
This module answers from outside NX instead, in about a second and with no NX call:

- the heartbeat file ``<workspace>/.nx-mcp/ui-state.json``: the interactive host
  writes it on its UI thread just before and just after every call (``running_method``,
  ``running_part``, ``running_started_at``, ``running_operation_id``) and about twice
  a second while idle (``sampled_at``). A step that holds the UI thread therefore
  leaves the file saying what runs since when; an idle host keeps it fresh.
- the NX process (pid from the bridge descriptor): alive, its windows responding
  (``IsHungAppWindow``: no message is sent to NX) and its CPU time advancing over a
  short sample (``GetProcessTimes``).

:func:`assess` combines them into one state:

- ``busy``: NX runs an agent call (tool, part, seconds); computing when its CPU time
  advances. Wait; one native step cannot be interrupted.
- ``blocked_by_dialog``: a modal NX dialog waits for the engineer.
- ``unresponsive``: NX runs but its UI thread neither answers nor runs an agent call
  (an engineer command, a long journal, or NX hangs), or a call stopped using CPU
  while NX's window does not respond.
- ``down``: no NX MCP host is registered or its NX process is gone.
- ``not_answering``: NX runs, the host is idle and its heartbeat fresh, yet the bridge
  did not accept the connection; the message says when a development reload of the host
  is asked for or just done (:func:`reload_state`), never otherwise.
- ``unknown``: no heartbeat and the process cannot be checked (off Windows).

NX-free (standard library and ctypes only): the sidecar imports it. Off Windows the
process checks report None (unknown) and only the heartbeat file speaks.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from nx_mcp.host import dialogs

STATE_FILE = "ui-state.json"
# An idle interactive host rewrites the heartbeat about twice a second; this much
# silence with no agent call running means its UI thread is held by something else.
STALE_SECONDS = 15.0
SAMPLE_SECONDS = 1.0  # CPU sample: how long to watch NX's CPU time
CPU_BUSY_FRACTION = 0.2  # of one core over the sample: NX computes


def state_file(root) -> Path:
    return Path(root) / ".nx-mcp" / STATE_FILE


# A development reload (scripts/dev_reload.py): it asks with the RESTART_FILE marker, the
# startup application removes it when it restarts the host and writes DEV_STATE_FILE
# (``generation``, ``updated_at``, ``ok``) once the new host runs or failed to start.
RESTART_FILE = "dev-restart"
DEV_STATE_FILE = "dev-state.json"
RELOAD_SECONDS = 10.0  # a host this recently (re)started may not listen yet


def reload_state(root, *, now: float | None = None) -> dict | None:
    """``{pending, generation, seconds}`` while a development host reload is asked for
    (``pending``) or finished less than RELOAD_SECONDS ago, else None. Never raises."""
    if root is None:
        return None
    folder = Path(root) / ".nx-mcp"
    now = time.time() if now is None else now
    try:
        pending = (folder / RESTART_FILE).exists()
    except OSError:
        pending = False
    state: dict = {}
    try:
        loaded = json.loads((folder / DEV_STATE_FILE).read_text(encoding="utf-8"))
        state = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError, TypeError):
        state = {}
    updated = state.get("updated_at")
    age = now - updated if isinstance(updated, (int, float)) else None
    generation = state.get("generation")
    # Generation 1 is NX's own start of the host, not a reload.
    restarted = isinstance(generation, int) and generation > 1 and bool(state.get("running"))
    recent = restarted and age is not None and 0 <= age <= RELOAD_SECONDS
    if not pending and not recent:
        return None
    return {
        "pending": pending,
        "generation": generation,
        "seconds": round(age, 1) if age is not None else None,
    }


def fresh(root, pid: int | None, *, now: float | None = None) -> bool:
    """Whether NX's host is alive by its heartbeat: written within STALE_SECONDS, or it
    says a call runs (the UI thread is held then, so the file stands still)."""
    beat = heartbeat_for(root, pid) if root is not None else None
    if not beat:
        return False
    if beat.get("running_method"):
        return True
    sampled = beat.get("sampled_at")
    now = time.time() if now is None else now
    return isinstance(sampled, (int, float)) and now - sampled <= STALE_SECONDS


def read(root) -> dict | None:
    """The host's last heartbeat (its status), or None when absent or unreadable."""
    try:
        data = json.loads(state_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def cpu_seconds(pid: int | None) -> float | None:
    """User + kernel CPU time of process ``pid`` in seconds (None: unknown)."""
    if not pid or sys.platform != "win32":
        return None
    try:  # pragma: no cover - needs Windows
        import ctypes
        from ctypes import wintypes as w

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [w.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            ticks = sum((t.dwHighDateTime << 32) | t.dwLowDateTime for t in times[2:])
            return ticks / 1e7  # 100-ns units
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        return None


def responding(pid: int | None) -> bool | None:
    """False when a visible top-level window of ``pid`` is hung (its thread has not
    pumped messages for about 5 s), True when none is, None when unknown or no window.
    ``IsHungAppWindow`` sends no message to NX."""
    if not pid or sys.platform != "win32":
        return None
    try:  # pragma: no cover - needs Windows
        import ctypes
        from ctypes import wintypes as w

        user = ctypes.windll.user32
        callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        seen: list[bool] = []

        def top(hwnd, _):
            owner = w.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user.IsWindowVisible(hwnd):
                seen.append(bool(user.IsHungAppWindow(hwnd)))
            return True

        user.EnumWindows(callback(top), 0)
        if not seen:
            return None
        return not any(seen)
    except Exception:
        return None


def probe(pid: int | None, *, sample: float = SAMPLE_SECONDS) -> dict:
    """``{pid, alive, responding, cpu_busy}`` of the NX process; None = unknown.
    ``cpu_busy`` watches the CPU time for ``sample`` seconds (0: not sampled)."""
    alive = dialogs.process_alive(pid)
    row: dict[str, Any] = {"pid": pid, "alive": alive, "responding": None, "cpu_busy": None}
    if not alive:
        return row
    row["responding"] = responding(pid)
    if sample > 0:
        first = cpu_seconds(pid)
        if first is not None:
            time.sleep(sample)
            second = cpu_seconds(pid)
            if second is not None:
                row["cpu_busy"] = second - first >= CPU_BUSY_FRACTION * sample
    return row


def duration(seconds: float) -> str:
    """``157 s``, ``12 min``, ``1 h 05 min``."""
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def _part_name(beat: dict) -> str | None:
    part = beat.get("running_part")
    if not part:
        parts = beat.get("parts")
        work = parts.get("work") if isinstance(parts, dict) else None
        part = work.get("path") if isinstance(work, dict) else None
    if not isinstance(part, str) or not part:
        return None
    return part.replace("\\", "/").rsplit("/", 1)[-1]


def running(beat: dict | None, *, now: float | None = None, reader: str | None = None):
    """``{tool, part, operation_id, seconds, since}`` of the call the heartbeat says runs,
    else None. ``operation_id`` shows only to the ``reader`` that names it (another
    agent's id is never shown)."""
    if not beat or not beat.get("running_method"):
        return None
    now = time.time() if now is None else now
    since = beat.get("running_started_at")
    if not isinstance(since, (int, float)):
        sampled = beat.get("sampled_at")
        elapsed = beat.get("operation_elapsed_seconds")
        since = (
            sampled - (elapsed if isinstance(elapsed, (int, float)) else 0.0)
            if isinstance(sampled, (int, float))
            else None
        )
    operation_id = beat.get("running_operation_id")
    return {
        "tool": str(beat["running_method"]),
        "part": _part_name(beat),
        "operation_id": operation_id if operation_id and operation_id == reader else None,
        "seconds": round(now - since) if since is not None else None,
        "since": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)) if since is not None else None
        ),
    }


def heartbeat_for(root, pid: int | None) -> dict | None:
    """The heartbeat when it belongs to NX process ``pid`` (a file left by an earlier
    NX session is ignored); any heartbeat when ``pid`` is unknown."""
    beat = read(root)
    if beat is None:
        return None
    if pid and beat.get("pid") and int(beat.get("pid") or 0) != int(pid):
        return None
    return beat


def running_call(root, pid: int | None, *, now: float | None = None) -> dict | None:
    """The call NX runs according to its heartbeat (no process check), else None."""
    return running(heartbeat_for(root, pid), now=now)


def assess(
    root,
    pid: int | None,
    *,
    registered: bool = True,
    reader: str | None = None,
    now: float | None = None,
    sample: float = SAMPLE_SECONDS,
) -> dict:
    """NX's state seen from outside: ``{state, pid, process, running, heartbeat_age_seconds,
    dialogs, message, hint}`` (module doc). ``registered``: a bridge descriptor exists;
    ``reader``: the asking call's operation_id. Never raises."""
    now = time.time() if now is None else now
    beat = heartbeat_for(root, pid) if registered else None
    pid = pid or (beat or {}).get("pid") or None
    process = (
        probe(pid, sample=sample)
        if registered
        else {"pid": pid, "alive": None, "responding": None, "cpu_busy": None}
    )
    sampled = (beat or {}).get("sampled_at")
    age = round(now - sampled, 1) if isinstance(sampled, (int, float)) else None
    call = running(beat, now=now, reader=reader)
    found = []
    if process["alive"]:
        found = [d.row() for d in dialogs.scan(pid, host_disabled=dialogs.ANY_OWNER)]
    health: dict[str, Any] = {
        "state": "unknown",
        "pid": pid,
        "process": {k: process[k] for k in ("alive", "responding", "cpu_busy")},
        "running": call,
        "heartbeat_age_seconds": age,
        "dialogs": found,
    }
    reloading = reload_state(root, now=now) if registered else None
    health.update(_verdict(registered, process, call, age, found, reloading))
    return health


def _doing(call: dict) -> str:
    where = f" on {call['part']}" if call.get("part") else ""
    took = f" for {duration(call['seconds'])}" if call.get("seconds") is not None else ""
    return f"{call['tool']}{where}{took}"


def _verdict(registered, process, call, age, found, reloading=None) -> dict:
    pid, alive = process.get("pid"), process.get("alive")
    if not registered:
        return {
            "state": "down",
            "message": "No NX MCP host is registered (no bridge descriptor): NX with the NX "
            "MCP host is not running",
            "hint": "Start NX with the NX MCP host, or ask the engineer",
        }
    if alive is False:
        return {
            "state": "down",
            "message": f"NX (pid {pid}) is not running",
            "hint": "Start NX with the NX MCP host, or ask the engineer; check "
            "nx_get_operation before retrying a changing call",
        }
    if found:
        title = found[0].get("title") or "an NX dialog"
        return {
            "state": "blocked_by_dialog",
            "message": f"A modal NX dialog (“{title}”) waits for the engineer"
            + (f"; NX holds {_doing(call)}" if call else ""),
            "hint": dialogs.hint_for(
                found[0].get("title"),
                "Ask the engineer to answer the NX dialog (the agent never clicks it)",
            )
            + "; nx_get_status shows when NX is free",
        }
    hung = process.get("responding") is False
    if call:
        computing = process.get("cpu_busy")
        if computing is False and hung:
            return {
                "state": "unresponsive",
                "message": f"NX (pid {pid}) runs {_doing(call)} but uses no CPU and its "
                "window does not respond: it may wait on something or hang",
                "hint": "Ask the engineer before anything else; do not resend calls and never "
                "kill NX yourself. nx_get_status answers at once meanwhile",
            }
        state = "computing" if computing else "running"
        return {
            "state": "busy",
            "message": f"NX (pid {pid}) is {state} {_doing(call)}"
            + (" and accepts no call until that step ends" if hung else ""),
            "hint": f"Wait: one native NX step cannot be interrupted. Do not resend "
            f"{call['tool']} or other heavy reads; check nx_get_status again in a minute or "
            "two (it answers at once while NX is busy)",
        }
    if (age is None or age > STALE_SECONDS) and (hung or age is not None):
        silent = (
            f"its NX MCP host has been silent for {duration(age)}"
            if age is not None
            else "its window does not respond"
        )
        return {
            "state": "unresponsive",
            "message": f"NX (pid {pid}) runs but {silent} and no agent call runs: the "
            "engineer may be in an NX command, or NX hangs",
            "hint": "Ask the engineer whether NX is busy; do not resend calls and never kill "
            "NX yourself. nx_get_status answers at once meanwhile",
        }
    if alive is None and age is None:
        return {
            "state": "unknown",
            "message": "The NX MCP host writes no heartbeat and its NX process cannot be "
            "checked from here",
            "hint": "Retry in a few seconds, or ask the engineer whether NX runs",
        }
    if reloading:
        return {
            "state": "not_answering",
            "message": f"NX (pid {pid}) runs and its NX MCP host is reloading (development reload)",
            "hint": "Retry in a few seconds; nx_get_status shows when the bridge answers again",
        }
    return {
        "state": "not_answering",
        "message": f"NX (pid {pid}) runs and its NX MCP host is idle, but the bridge did not "
        "accept the connection",
        "hint": "Retry in a few seconds; nx_get_status shows when the bridge answers again",
    }

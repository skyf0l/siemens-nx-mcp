"""Serialize live NX sessions between parallel development agents.

There is one NX. A dev reload restarts the host for everyone and resets object
IDs, so only the lock owner may run dev_reload.py, dev_python.py or the acceptance
runner (`python -m nx_mcp.acceptance run|call`, which takes the lock itself).

    python scripts/dev_lock.py acquire --owner holes      # waits; exit 0 = yours
    python scripts/dev_lock.py refresh --owner holes      # while you use NX
    python scripts/dev_lock.py release --owner holes
    python scripts/dev_lock.py status

The holder refreshes the lock while it uses NX: the acceptance runner does it every
30 s for its whole run; a manual holder runs `refresh` (or `acquire` again) at least
every few minutes. A lock not refreshed for --stale minutes (default 10) is stale:
the next acquire takes it over and names whose lock it took. A waiting acquire
gives up (exit 3) when the process that started it has exited, so the waiter of a
dead agent never takes the lock later.

Exit codes: 0 done, 1 not held by --owner (refresh, release), 2 timed out,
3 the waiting process's parent exited.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path

STALE_SECONDS = 600.0  # a lock not refreshed this long may be taken over
BEAT_SECONDS = 30.0  # how often the acceptance runner refreshes the lock it holds
POLL_SECONDS = 2.0  # how often a waiting acquire looks again
GUARD_TIMEOUT = 30.0  # seconds to wait for the short guard around every lock change


def lock_path(workspace: str) -> Path:
    return Path(workspace) / ".nx-mcp" / "dev.lock"


def read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def age(current: dict | None, now: float | None = None) -> float:
    """Seconds since ``current`` was last refreshed."""
    stamp = (current or {}).get("refreshed_at", 0)
    try:
        return (time.time() if now is None else now) - float(stamp)
    except (TypeError, ValueError):
        return float("inf")


def describe(current: dict | None, stale_seconds: float = STALE_SECONDS) -> str:
    if not current:
        return "free"
    seconds = age(current)
    note = " - stale, the next acquire takes it over" if seconds > stale_seconds else ""
    return f"held by {current.get('owner')} (refreshed {seconds:.0f} s ago{note})"


@contextlib.contextmanager
def guard(path: Path, timeout: float = GUARD_TIMEOUT) -> Iterator[None]:
    """An OS file lock around one read-change-write of ``path``: two agents taking over
    the same stale lock cannot both win. The OS frees it when its process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".guard"), "a+b") as stream:
        deadline = time.monotonic() + timeout
        if sys.platform == "win32":
            import msvcrt

            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{stream.name} stays locked") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{stream.name} stays locked") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def write(path: Path, data: dict) -> None:
    """Replace the lock file in one step: a reader never sees half a file."""
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temp, path)


def try_acquire(
    path: Path, owner: str, stale_seconds: float = STALE_SECONDS
) -> tuple[bool, str | None]:
    """``(acquired, note)``. The owner's own lock is refreshed (note ``refreshed``); a
    stale lock of another owner is taken over (the note names it)."""
    with guard(path):
        now = time.time()
        current = read(path)
        if current and current.get("owner") == owner:
            write(path, {**current, "refreshed_at": now, "pid": os.getpid()})
            return True, "refreshed"
        note = None
        if current:
            seconds = age(current, now)
            if seconds <= stale_seconds:
                return False, None
            note = (
                f"took over the stale lock of {current.get('owner')} "
                f"(last refreshed {seconds:.0f} s ago)"
            )
        elif path.exists():
            note = "replaced an unreadable lock file"
        write(
            path,
            {
                "owner": owner,
                "acquired_at": now,
                "refreshed_at": now,
                "pid": os.getpid(),
                "host": socket.gethostname(),
            },
        )
        return True, note


def refresh(path: Path, owner: str) -> bool:
    """Refresh ``owner``'s lock; False (and no change) when ``owner`` does not hold it."""
    with guard(path):
        current = read(path)
        if not current or current.get("owner") != owner:
            return False
        write(path, {**current, "refreshed_at": time.time()})
        return True


def release(path: Path, owner: str) -> bool:
    """Remove ``owner``'s lock; False (and no change) when ``owner`` does not hold it."""
    with guard(path):
        current = read(path)
        if not current or current.get("owner") != owner:
            return False
        path.unlink(missing_ok=True)
        return True


MAX_ANCESTORS = 6  # how far up a waiting acquire watches the processes that started it


class ParentWatch:
    """Whether the processes that started this one still run (``alive``).

    Windows: this process's ancestors (up to ``MAX_ANCESTORS``: the venv's python.exe
    launcher, the shell, the agent's process), each by a handle opened at start and kept,
    so a PID cannot be reused meanwhile; the walk stops at an ancestor it cannot open or
    one younger than its child (a reused PID). ``pids`` watches those processes instead.
    Elsewhere: the parent PID changes when the parent exits. Unwatchable: always alive.
    ``pid`` names the watched process that exited."""

    def __init__(self, pids: list[int] | None = None) -> None:
        self.pid: int | None = None
        self._handles: list[tuple[int, object]] = []
        self._parent = os.getppid()
        self._windows: bool = sys.platform == "win32"
        if sys.platform == "win32":
            with contextlib.suppress(Exception):  # a courtesy: never fails the wait
                self._handles = _open_processes(pids)
        elif pids:
            self._parent = pids[0]

    def alive(self) -> bool:
        if self._windows:
            for pid, handle in self._handles:
                if _exited(handle):
                    self.pid = pid
                    return False
            return True
        if self._parent > 1 and os.getppid() != self._parent:
            self.pid = self._parent
            return False
        return True

    def close(self) -> None:
        handles, self._handles = self._handles, []
        for _pid, handle in handles:
            _kernel32().CloseHandle(handle)


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, dword = wintypes.HANDLE, wintypes.DWORD
    kernel32.OpenProcess.restype = handle
    kernel32.OpenProcess.argtypes = [dword, wintypes.BOOL, dword]
    kernel32.WaitForSingleObject.argtypes = [handle, dword]
    kernel32.WaitForSingleObject.restype = dword
    kernel32.CloseHandle.argtypes = [handle]
    kernel32.GetCurrentProcess.restype = handle
    kernel32.GetProcessTimes.argtypes = [handle] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.CreateToolhelp32Snapshot.restype = handle
    kernel32.CreateToolhelp32Snapshot.argtypes = [dword, dword]
    return kernel32


def _created(kernel32, handle) -> int | None:
    from ctypes import byref, wintypes

    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel32.GetProcessTimes(handle, *(byref(t) for t in times)):
        return None
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


def _parents() -> dict[int, int]:
    """``{pid: parent pid}`` of every process (a Toolhelp snapshot)."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = _kernel32()
    for name in ("Process32FirstW", "Process32NextW"):
        getattr(kernel32, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return {}
    out: dict[int, int] = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            out[entry.th32ProcessID] = entry.th32ParentProcessID
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return out


def _open_processes(pids: list[int] | None) -> list[tuple[int, object]]:
    """Handles to ``pids``, or to this process's ancestors (see :class:`ParentWatch`)."""
    kernel32 = _kernel32()
    access = 0x00100000 | 0x1000  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    handles: list[tuple[int, object]] = []
    if pids is not None:
        for pid in pids:
            handle = kernel32.OpenProcess(access, False, pid)
            if handle:
                handles.append((pid, handle))
        return handles
    parents = _parents()
    child, born = os.getpid(), _created(kernel32, kernel32.GetCurrentProcess())
    for _ in range(MAX_ANCESTORS):
        pid = parents.get(child, 0)
        if pid in (0, 4) or pid == child:  # the idle and system processes
            break
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            break
        created = _created(kernel32, handle)
        if created is None or born is None or created > born:  # not our ancestor
            kernel32.CloseHandle(handle)
            break
        handles.append((pid, handle))
        child, born = pid, created
    return handles


def _exited(handle) -> bool:
    return bool(_kernel32().WaitForSingleObject(handle, 0) == 0)  # WAIT_OBJECT_0: signalled


def acquire(
    path: Path,
    owner: str,
    *,
    wait: float,
    stale_seconds: float = STALE_SECONDS,
    parent: ParentWatch | None = None,
    poll: float = POLL_SECONDS,
    echo=print,
) -> int:
    """The ``acquire`` command: 0 acquired, 2 timed out, 3 the parent exited."""
    deadline = time.monotonic() + wait
    waiting = False
    while True:
        acquired, note = try_acquire(path, owner, stale_seconds)
        if acquired:
            echo(f"acquired by {owner}" + (f" ({note})" if note and note != "refreshed" else ""))
            return 0
        if time.monotonic() > deadline:
            echo(f"timed out; {describe(read(path), stale_seconds)}")
            return 2
        if parent is not None and not parent.alive():
            echo(f"gave up: process {parent.pid}, which started this wait, has exited")
            return 3
        if not waiting:
            echo(f"waiting: {describe(read(path), stale_seconds)}")
            waiting = True
        time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["acquire", "refresh", "release", "status"])
    parser.add_argument("--owner")
    parser.add_argument("--wait", type=float, default=3600, help="seconds to wait for the lock")
    parser.add_argument(
        "--stale",
        type=float,
        default=STALE_SECONDS / 60,
        help="minutes without a refresh before a lock may be taken over",
    )
    # The default of nx_mcp.workspace.default_workspace(), without importing nx_mcp.
    parser.add_argument(
        "--workspace",
        default=os.environ.get("NX_MCP_WORKSPACE") or str(Path.home() / "NX_MCP_WORKSPACE"),
    )
    args = parser.parse_args(argv)
    path = lock_path(args.workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = args.stale * 60
    if args.action == "status":
        print(describe(read(path), stale))
        return 0
    if not args.owner:
        parser.error("--owner is required")
    if args.action in ("refresh", "release"):
        done = (refresh if args.action == "refresh" else release)(path, args.owner)
        if done:
            print("refreshed" if args.action == "refresh" else "released")
            return 0
        print(f"not held by {args.owner}: {describe(read(path), stale)}")
        return 1
    watch = ParentWatch()
    try:
        return acquire(path, args.owner, wait=args.wait, stale_seconds=stale, parent=watch)
    finally:
        watch.close()


if __name__ == "__main__":
    sys.exit(main())

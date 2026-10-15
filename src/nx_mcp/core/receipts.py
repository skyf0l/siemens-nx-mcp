"""Receipts: fingerprint, idempotent replay, states, outcome, progress, cancel flag.

A receipt keeps the request's arguments (``arguments``) so ``nx_get_operation`` can
show what was asked: file contents (``content``, ``data_base64``) and other long
strings are replaced by a size note, and the copy is bounded (:func:`redact`).

Gate 7. The records live in an :class:`OperationStore`
(``.nx-mcp/operations/<operation_id>.json``), which ``nx_get_operation`` and
``nx_cancel_operation`` (domains/session/status.py) read from either process.
Repeating an operation_id replays the finished call, except a committed call that
``nx_undo_operation`` or ``nx_restore_checkpoint`` reverted: it runs again, and its
new receipt links the reverted run (``previous``).
``store`` needs ``get(op_id)``, ``put(record)`` and ``path(op_id)``; running receipts
from an earlier session are turned into ``unknown`` by the store's ``recover()``, which
reads the ``running/`` markers only and starts the retention pruner
(:class:`OperationStore`: ``RETENTION_DAYS``, ``MAX_RECEIPTS``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

from nx_mcp.core.errors import NXError, fail

TRACE_LOG_BYTES = 1_000_000


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_operation_id() -> str:
    return "op_" + uuid.uuid4().hex


# An operation_id is optional (the server makes one); a caller's own id must match this.
OPERATION_ID_MIN, OPERATION_ID_MAX = 8, 128
OPERATION_ID_CHARS = r"^[A-Za-z0-9_-]+$"
OPERATION_ID_RULE = "8-128 characters from A-Z a-z 0-9 _ -"


def operation_id_problem(value) -> str | None:
    """Why ``value`` is not a valid operation_id (None when it is)."""
    if not isinstance(value, str):
        return f"operation_id must be a string of {OPERATION_ID_RULE}"
    if len(value) < OPERATION_ID_MIN:
        return (
            f"operation_id {value!r} is too short: {len(value)} of at least "
            f"{OPERATION_ID_MIN} characters ({OPERATION_ID_RULE})"
        )
    if len(value) > OPERATION_ID_MAX:
        return f"operation_id is too long: {len(value)} of at most {OPERATION_ID_MAX} characters"
    bad = sorted({c for c in value if not re.match(OPERATION_ID_CHARS, c)})
    if bad:
        return f"operation_id {value!r} has characters outside A-Z a-z 0-9 _ -: " + " ".join(
            repr(c) for c in bad
        )
    return None


OPERATION_ID_HINT = (
    "operation_id is optional: omit it and the server makes one. Pass your own "
    f"({OPERATION_ID_RULE}, e.g. 'fan_blade_01') only for a mutation you may retry"
)


def fingerprint(tool: str, args: dict, part: str | None = None) -> str:
    """SHA-256 of the tool name and canonical JSON arguments, and of the target part's
    file key (gate 3t) when the call has one: the same arguments in another part are
    another call. Without a part it is the fingerprint receipts had before targets."""
    body: list = [tool, args] if part is None else [tool, args, part]
    try:
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise NXError(
            "NX_INVALID_ARGUMENT",
            "Arguments must be finite JSON values",
            details={"error": str(error)},
        ) from error
    return hashlib.sha256(text.encode()).hexdigest()


ANY_PART = object()  # Receipts.replay: the target is unknown; match the receipt's own part


def same_call(existing: dict, tool: str, args: dict, part) -> bool:
    """Whether a receipt records this call: the same tool and arguments in the same
    target part. A receipt written before targets (no ``part_file``) matches the tool
    and arguments alone, so a repeat replays it as before (receipts written before target parts)."""
    stored = existing.get("fingerprint")
    if part is ANY_PART:
        return stored == fingerprint(tool, args, existing.get("part_file"))
    if stored == fingerprint(tool, args, part):
        return True
    return "part_file" not in existing and stored == fingerprint(tool, args)


def _conflict(existing: dict, tool: str, args: dict, operation_id: str, part) -> NoReturn:
    other = existing.get("part_file")
    if other is not None and other != part and same_call(existing, tool, args, ANY_PART):
        name = str(other).replace("\\", "/").rsplit("/", 1)[-1]
        fail(
            "NX_IDEMPOTENCY_CONFLICT",
            f"operation_id {operation_id} was already used for the same call in another "
            f"part ({name})",
            field="/operation_id",
            hint="Use a new operation_id for each part, e.g. prefix it with the part's name",
            details={"operation_id": operation_id, "part": name, "part_path": other},
            retry="after_fix",
        )
    fail(
        "NX_IDEMPOTENCY_CONFLICT",
        f"operation_id {operation_id} was already used by another call ("
        f"{existing.get('tool') or 'another tool'}) with different arguments",
        field="/operation_id",
        hint="This id already ran with other arguments: use a new operation_id for a "
        "different call (nx_get_operation shows the earlier call); repeat the same id only "
        "with the same arguments to retry it",
        details={"operation_id": operation_id, "tool": existing.get("tool")},
        retry="after_fix",
    )


# Arguments that carry file contents: never stored in a receipt.
CONTENT_FIELDS = frozenset({"content", "data_base64", "content_base64", "base64"})
LONG_TEXT = 400  # characters of a string argument kept in a receipt
MAX_ARGUMENTS = 8000  # characters of the stored arguments (JSON)


def redact(args, _key: str | None = None):
    """A receipt copy of ``args``: file contents and long strings become a size note,
    lists keep their first 100 items; a copy over ``MAX_ARGUMENTS`` characters is
    replaced by ``{"omitted": <characters>}``."""

    def walk(value, key=None):
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            out = [walk(v, key) for v in value[:100]]
            if len(value) > 100:
                out.append(f"<{len(value) - 100} more items>")
            return out
        if isinstance(value, str):
            if key in CONTENT_FIELDS:
                return f"<file content omitted: {len(value)} characters>"
            if len(value) > LONG_TEXT:
                return value[:LONG_TEXT] + f"... <{len(value) - LONG_TEXT} characters omitted>"
        return value

    copy = walk(args, _key)
    try:
        size = len(json.dumps(copy, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {"omitted": "not JSON"}
    return copy if size <= MAX_ARGUMENTS else {"omitted": size}


REVERTED_RUNS = 5  # reverted runs of one operation_id kept in its receipt (newest first)


def previous_run(existing: dict) -> dict | None:
    """The reverted run a re-run of the same operation_id links to, or None: when it
    started and finished, what reverted it (an undo's operation_id or a checkpoint id),
    the refs it created, and the runs before it (at most ``REVERTED_RUNS``)."""
    if not existing.get("reverted_by") or existing.get("state") != "committed":
        return None
    created = ((existing.get("envelope") or {}).get("changes") or {}).get("created") or []
    run = {
        "started_at": existing.get("started_at"),
        "finished_at": existing.get("finished_at"),
        "reverted_by": existing["reverted_by"],
        "created": [r.get("id") for r in created if isinstance(r, dict)][:20],
    }
    earlier = existing.get("previous")
    if isinstance(earlier, dict):
        chain = [earlier, *(earlier.get("earlier") or [])]
        run["earlier"] = [{k: v for k, v in r.items() if k != "earlier"} for r in chain][
            : REVERTED_RUNS - 1
        ]
    return run


RETENTION_DAYS = 7  # receipts older than this (by mtime) are pruned
MAX_RECEIPTS = 20_000  # at most this many receipts are kept: the newest by mtime
PRUNE_PAUSE_EVERY = 500  # deletions between short pauses of the background pruner
_prune_lock = threading.Lock()  # one pruner per process (a dev reload runs recover again)


class OperationStore:
    """Durable receipts as JSON files. No NXOpen calls; safe for sidecar status queries.

    A receipt in state ``running`` also has an empty marker ``running/<operation_id>``
    (written before the receipt, removed after the receipt leaves ``running``), so
    :meth:`recover` reads the in-flight operations only, never the whole history.
    Receipts written before the markers existed have none: they are finished, or running
    receipts of a session that crashed under the older version. Those keep ``running``
    (a repeat is ``NX_OUTCOME_UNKNOWN``, as for any unknown outcome) until retention
    prunes them; no one-time full scan is made at start.

    Retention (:meth:`prune`): receipts older than ``RETENTION_DAYS`` and all but the
    ``MAX_RECEIPTS`` newest are deleted by a daemon thread :meth:`recover` starts;
    receipts with a running marker are never pruned. A pruned receipt reads as
    ``unknown`` in :meth:`get`, so repeating a pruned operation_id runs the call again:
    idempotent replay is meant for recent retries, not for week-old calls.
    """

    def __init__(self, root):
        self.root = Path(root) / ".nx-mcp" / "operations"
        self.root.mkdir(parents=True, exist_ok=True)
        self.running = self.root / "running"  # in-flight markers, made on first use
        self.pruner: threading.Thread | None = None

    def path(self, operation_id):
        problem = operation_id_problem(operation_id)
        if problem is not None:
            raise NXError("NX_INVALID_ARGUMENT", problem, hint=OPERATION_ID_HINT)
        return self.root / (operation_id + ".json")

    def get(self, operation_id):
        """The receipt, or an ``unknown`` record when there is none (never written, or
        pruned by retention: :meth:`prune`)."""
        path = self.path(operation_id)
        if not path.exists():
            return {
                "operation_id": operation_id,
                "state": "unknown",
                "mutation_outcome": "unknown",
                "reason": "No durable receipt exists; do not infer failure.",
            }
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            raise NXError("NX_TRANSFER_FAILED", str(e)) from e

    def put(self, record):
        path = self.path(record["operation_id"])
        marker = self.running / record["operation_id"]
        running = record.get("state") == "running"
        if running and not marker.exists():  # before the receipt: a crash leaves a marker
            self.running.mkdir(exist_ok=True)
            marker.touch()
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        if not running:  # after the receipt; recover drops a marker left behind
            with contextlib.suppress(OSError):
                marker.unlink(missing_ok=True)

    def recover(self, session_id, *, prune: bool = True) -> None:
        """Turn the running receipts of other sessions into ``unknown``. Reads the
        ``running/`` markers and the receipts they name only (cost follows the in-flight
        operations, not the history), then starts the background :meth:`prune`
        (``prune=False``: not). Never raises."""
        try:
            names = os.listdir(self.running) if self.running.is_dir() else []
        except OSError:
            names = []
        for name in names:
            with contextlib.suppress(Exception):
                self._recover_one(name, session_id)
        if prune:
            self.start_pruning()

    def _recover_one(self, name: str, session_id) -> None:
        try:
            record = json.loads((self.root / (name + ".json")).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        if not isinstance(record, dict) or record.get("state") != "running":
            (self.running / name).unlink(missing_ok=True)  # finished or gone: stale marker
        elif record.get("session_id") != session_id:
            record.update(
                state="unknown",
                mutation_outcome="unknown",
                reason="NX process restarted before final receipt; manual reconciliation required.",
            )
            self.put(record)

    def start_pruning(self) -> threading.Thread | None:
        """Run :meth:`prune` in a daemon thread (NX's main thread never waits for it);
        None while another pruner of this process runs."""
        if not _prune_lock.acquire(blocking=False):
            return None

        def run() -> None:
            try:
                self.prune()
            finally:
                _prune_lock.release()

        try:
            thread = threading.Thread(target=run, name="nx-mcp-receipt-prune", daemon=True)
            thread.start()
        except Exception:
            _prune_lock.release()
            return None
        self.pruner = thread
        return thread

    def prune(
        self,
        *,
        now: float | None = None,
        max_age_days: float = RETENTION_DAYS,
        max_count: int = MAX_RECEIPTS,
        pause: float = 0.01,
    ) -> int:
        """Delete the receipts older than ``max_age_days`` and all but the ``max_count``
        newest (by mtime), with their ``.cancel`` flags, and ``.tmp`` files older than
        ``max_age_days``; never a receipt with a running marker. Reads directory
        entries only (no receipt is parsed), keeps a file rewritten since the scan
        (concurrent ``put``), never raises. Returns the number of files deleted."""
        deleted = 0
        with contextlib.suppress(Exception):
            now = time.time() if now is None else now
            cutoff = now - max_age_days * 86400
            receipts: list[tuple[float, str]] = []  # (mtime, operation_id)
            extras: list[tuple[float, str, str]] = []  # (mtime, operation_id, file name)
            with os.scandir(self.root) as entries:
                for entry in entries:
                    stem, _, suffix = entry.name.rpartition(".")
                    if suffix not in ("json", "cancel", "tmp") or not stem:
                        continue
                    with contextlib.suppress(OSError):
                        if not entry.is_file():
                            continue
                        mtime = entry.stat().st_mtime
                        if suffix == "json":
                            receipts.append((mtime, stem))
                        else:
                            extras.append((mtime, stem, entry.name))
            receipts.sort(reverse=True)  # newest first
            doomed = {
                stem: mtime
                for i, (mtime, stem) in enumerate(receipts)
                if mtime < cutoff or i >= max_count
            }
            files = [(stem + ".json", mtime) for stem, mtime in doomed.items()]
            files += [
                (name, mtime)
                for mtime, stem, name in extras
                if mtime < cutoff or (stem in doomed and name.endswith(".cancel"))
            ]
            for name, mtime in files:
                if self._delete(name, mtime):
                    deleted += 1
                    if pause and deleted % PRUNE_PAUSE_EVERY == 0:
                        time.sleep(pause)  # let NX's Python thread have the interpreter
        return deleted

    def _delete(self, name: str, scanned: float) -> bool:
        """Delete one file of the store unless its operation is running or the file
        changed since the scan (``put`` writes the marker before a running receipt)."""
        try:
            path = self.root / name
            if (self.running / name.rpartition(".")[0]).exists():
                return False
            if path.stat().st_mtime > scanned:
                return False
            path.unlink()
            return True
        except OSError:
            return False


class Receipts:
    def __init__(self, store, session_id: str) -> None:
        self.store = store
        self.session_id = session_id

    def check_id(self, operation_id: str) -> None:
        problem = operation_id_problem(operation_id)
        if problem is not None:
            raise NXError(
                "NX_INVALID_ARGUMENT", problem, field="/operation_id", hint=OPERATION_ID_HINT
            )

    def replay(
        self, tool: str, args: dict, operation_id: str, part: str | None | object = None
    ) -> dict | None:
        """The replayed envelope of a finished operation with this id, else None: also
        None for a committed operation that an undo or a restore reverted (its receipt has
        ``reverted_by``), so repeating it recreates the work instead of replaying a result
        that no longer exists.

        ``part``: the target part's file key (gate 3t), None without a target, or
        :data:`ANY_PART` when the target could not be resolved (a replay then matches the
        receipt's own part). Raises ``NX_IDEMPOTENCY_CONFLICT`` for the same id with other
        arguments or in another part and ``NX_OUTCOME_UNKNOWN`` while it is running or
        unknown. The kernel asks before the workspace-path gate, so repeating a committed
        ``nx_create_part`` replays its result instead of failing on the file it created.
        """
        self.check_id(operation_id)
        existing = self.store.get(operation_id)
        if "fingerprint" in existing:
            if not same_call(existing, tool, args, part):
                _conflict(existing, tool, args, operation_id, part)
            state = existing.get("state")
            stored = existing.get("envelope")
            if existing.get("reverted_by") and state == "committed":
                rerun = self._redone(existing)
                if rerun is not None:
                    return rerun
                return None  # undone: the call runs again (begin links the new receipt)
            if state in ("committed", "failed") and isinstance(stored, dict):
                replay = dict(stored, replayed=True)
                warnings = list(replay.get("warnings") or [])
                if existing.get("session_id") != self.session_id:
                    warnings.append(
                        {
                            "code": "NX_UPDATE_WARNING",
                            "message": "Receipt is from an earlier NX session; reacquire references",
                        }
                    )
                replay["warnings"] = warnings
                return replay
            fail(
                "NX_OUTCOME_UNKNOWN",
                f"Operation {operation_id} is {state}; query nx_get_operation before retrying",
                details={"operation_id": operation_id, "state": state},
            )
        return None

    def _redone(self, existing: dict) -> dict | None:
        """The replay of an undone call that ``nx_redo_operation`` ran again under another
        operation_id (``redone_as``: its refs were remapped), while that run stands: a
        retry of the old id must not make the objects a second time."""
        other = existing.get("redone_as")
        if not isinstance(other, str):
            return None
        try:
            rerun = self.store.get(other)
        except Exception:
            return None
        stored = rerun.get("envelope")
        if (
            rerun.get("state") != "committed"
            or rerun.get("reverted_by")
            or not isinstance(stored, dict)
        ):
            return None
        replay = dict(stored, replayed=True)
        replay["warnings"] = [
            *(replay.get("warnings") or []),
            {
                "code": "NX_NOTE",
                "message": f"{existing.get('operation_id')} was undone and redone as {other}: "
                "this is that run's result",
            },
        ]
        return replay

    def begin(
        self,
        tool: str,
        args: dict,
        operation_id: str,
        part_key: str | None,
        *,
        part_file: str | None = None,
    ) -> tuple[dict | None, dict | None]:
        """``(record, None)`` for a new operation or ``(None, replayed_envelope)``.

        ``part_file``: the target part's file key (in the fingerprint and the receipt).
        An operation an undo or a checkpoint restore reverted runs again under its id: the
        new receipt keeps the reverted run under ``previous`` (:func:`previous_run`)."""
        replayed = self.replay(tool, args, operation_id, part_file)
        if replayed is not None:
            return None, replayed
        digest = fingerprint(tool, args, part_file)
        earlier = previous_run(self.store.get(operation_id))
        record = {
            "operation_id": operation_id,
            "method": tool,
            "tool": tool,
            "fingerprint": digest,
            "session_id": self.session_id,
            "part_id": part_key,
            "part_file": part_file,
            "arguments": redact(args),
            "state": "running",
            "mutation_outcome": "unknown",
            "started_at": timestamp(),
        }
        if earlier is not None:
            record["previous"] = earlier
        self.store.put(record)
        return record, None

    def progress(self, record: dict | None, done=None, total=None) -> None:
        if record is None:
            return
        if total is None:  # a child's checkpoint keeps the parent's announced total
            total = (record.get("progress") or {}).get("total")
        record["progress"] = {"completed": done, "total": total, "at": timestamp()}
        self.store.put(record)

    def finish(self, record: dict | None, envelope: dict, trace: str | None = None) -> None:
        """Write the final state. Never raises: a committed result stays committed.
        ``trace``: the development traceback of a native failure (kept out of the
        envelope)."""
        if record is None:
            return
        if trace:
            record["traceback"] = trace
        record.update(
            state="committed" if envelope.get("ok") else "failed",
            mutation_outcome=envelope.get("mutation_outcome"),
            envelope=envelope,
            finished_at=timestamp(),
        )
        if envelope.get("ok"):
            record["result"] = envelope
        else:
            record["error"] = envelope.get("error")
        # A running receipt becomes unknown on restart; never undo a commit.
        with contextlib.suppress(Exception):
            self.store.put(record)

    def log_trace(self, tool: str, operation_id: str, trace: str) -> None:
        """Append a development traceback (``NX_MCP_DEV=1``) to
        ``.nx-mcp/logs/tracebacks.log`` next to the receipts: tracebacks never go into
        an agent-facing envelope. Never raises; the log is rotated at 1 MB."""
        root = getattr(self.store, "root", None)
        if root is None or not trace:
            return
        with contextlib.suppress(Exception):
            log = Path(root).parent / "logs" / "tracebacks.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            if log.exists() and log.stat().st_size > TRACE_LOG_BYTES:
                log.replace(log.with_suffix(".log.1"))
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"--- {timestamp()} {tool} {operation_id}\n{trace.rstrip()}\n")

    def cancel_requested(self, operation_id: str | None) -> bool:
        if not operation_id:
            return False
        try:
            return self.store.path(operation_id).with_suffix(".cancel").exists()
        except Exception:
            return False

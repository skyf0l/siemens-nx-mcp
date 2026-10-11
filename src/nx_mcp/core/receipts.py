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
from an earlier session are turned into ``unknown`` by the store's ``recover()``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

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


def fingerprint(tool: str, args: dict) -> str:
    """SHA-256 of the tool name and canonical JSON arguments."""
    try:
        text = json.dumps([tool, args], sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise NXError(
            "NX_INVALID_ARGUMENT",
            "Arguments must be finite JSON values",
            details={"error": str(error)},
        ) from error
    return hashlib.sha256(text.encode()).hexdigest()


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


class OperationStore:
    """Durable receipts as JSON files. No NXOpen calls; safe for sidecar status queries."""

    def __init__(self, root):
        self.root = Path(root) / ".nx-mcp" / "operations"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, operation_id):
        problem = operation_id_problem(operation_id)
        if problem is not None:
            raise NXError("NX_INVALID_ARGUMENT", problem, hint=OPERATION_ID_HINT)
        return self.root / (operation_id + ".json")

    def get(self, operation_id):
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
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)

    def recover(self, session_id):
        for path in self.root.glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("state") == "running" and record.get("session_id") != session_id:
                record.update(
                    state="unknown",
                    mutation_outcome="unknown",
                    reason="NX process restarted before final receipt; manual reconciliation required.",
                )
                self.put(record)


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

    def replay(self, tool: str, args: dict, operation_id: str) -> dict | None:
        """The replayed envelope of a finished operation with this id, else None: also
        None for a committed operation that an undo or a restore reverted (its receipt has
        ``reverted_by``), so repeating it recreates the work instead of replaying a result
        that no longer exists.

        Raises ``NX_IDEMPOTENCY_CONFLICT`` for the same id with other arguments and
        ``NX_OUTCOME_UNKNOWN`` while it is running or unknown. The kernel asks before
        the workspace-path gate, so repeating a committed ``nx_create_part`` replays its
        result instead of failing on the file it created.
        """
        self.check_id(operation_id)
        digest = fingerprint(tool, args)
        existing = self.store.get(operation_id)
        if "fingerprint" in existing:
            if existing["fingerprint"] != digest:
                fail(
                    "NX_IDEMPOTENCY_CONFLICT",
                    "operation_id was already used with different arguments",
                    details={"operation_id": operation_id},
                )
            state = existing.get("state")
            stored = existing.get("envelope")
            if existing.get("reverted_by") and state == "committed":
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

    def begin(
        self, tool: str, args: dict, operation_id: str, part_key: str | None
    ) -> tuple[dict | None, dict | None]:
        """``(record, None)`` for a new operation or ``(None, replayed_envelope)``.

        An operation an undo or a checkpoint restore reverted runs again under its id:
        the new receipt keeps the reverted run under ``previous`` (:func:`previous_run`)."""
        replayed = self.replay(tool, args, operation_id)
        if replayed is not None:
            return None, replayed
        digest = fingerprint(tool, args)
        earlier = previous_run(self.store.get(operation_id))
        record = {
            "operation_id": operation_id,
            "method": tool,
            "tool": tool,
            "fingerprint": digest,
            "session_id": self.session_id,
            "part_id": part_key,
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

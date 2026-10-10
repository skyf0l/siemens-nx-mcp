"""Receipts: fingerprint, idempotent replay, states, outcome, progress, cancel flag.

Gate 7. The records live in an :class:`OperationStore`
(``.nx-mcp/operations/<operation_id>.json``), which ``nx_get_operation`` and
``nx_cancel_operation`` (domains/session/status.py) read from either process.
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


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_operation_id() -> str:
    return "op_" + uuid.uuid4().hex


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


class OperationStore:
    """Durable receipts as JSON files. No NXOpen calls; safe for sidecar status queries."""

    def __init__(self, root):
        self.root = Path(root) / ".nx-mcp" / "operations"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{8,128}", operation_id
        ):
            raise NXError(
                "NX_INVALID_ARGUMENT",
                "operation_id must be 8–128 ASCII letters, digits, underscores or hyphens",
            )
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
        try:
            self.store.path(operation_id)
        except Exception as error:
            raise NXError(
                "NX_INVALID_ARGUMENT",
                "operation_id must be 8-128 ASCII letters, digits, underscores or hyphens",
                field="/operation_id",
            ) from error

    def replay(self, tool: str, args: dict, operation_id: str) -> dict | None:
        """The replayed envelope of a finished operation with this id, else None.

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
            if state in ("committed", "failed") and isinstance(stored, dict):
                replay = dict(stored, replayed=True)
                warnings = list(replay.get("warnings") or [])
                if existing.get("reverted_by"):
                    warnings.append(
                        {
                            "code": "NX_UPDATE_WARNING",
                            "message": "This operation was reverted by "
                            + str(existing["reverted_by"])
                            + "; replay does not recreate it",
                        }
                    )
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

    def begin(self, tool: str, args: dict, operation_id: str, part_key: str | None):
        """``(record, None)`` for a new operation or ``(None, replayed_envelope)``."""
        replayed = self.replay(tool, args, operation_id)
        if replayed is not None:
            return None, replayed
        digest = fingerprint(tool, args)
        record = {
            "operation_id": operation_id,
            "method": tool,
            "tool": tool,
            "fingerprint": digest,
            "session_id": self.session_id,
            "part_id": part_key,
            "state": "running",
            "mutation_outcome": "unknown",
            "started_at": timestamp(),
        }
        self.store.put(record)
        return record, None

    def progress(self, record: dict, done=None, total=None) -> None:
        if record is None:
            return
        if total is None:  # a child's checkpoint keeps the parent's announced total
            total = (record.get("progress") or {}).get("total")
        record["progress"] = {"completed": done, "total": total, "at": timestamp()}
        self.store.put(record)

    def finish(self, record: dict, envelope: dict) -> None:
        """Write the final state. Never raises: a committed result stays committed."""
        if record is None:
            return
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

    def cancel_requested(self, operation_id: str | None) -> bool:
        if not operation_id:
            return False
        try:
            return self.store.path(operation_id).with_suffix(".cancel").exists()
        except Exception:
            return False

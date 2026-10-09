"""Agent activity history for the NX MCP panel and the workspace activity log.

Standard library only: the NX-side control panel imports this. Each entry says
what ran (the request summary), what it created, and what happened to the
model when it failed. Read-only steps are grouped so they do not push model
changes out of the short panel history.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LINE_WIDTH = 84
LOG_LIMIT_BYTES = 5 * 1024 * 1024

# Failure outcome -> what the engineer needs to know about the model.
_MODEL_STATE = {
    "rolled_back": "rolled back, model unchanged",
    "not_started": "not run, model unchanged",
    "not_applicable": "model unchanged",  # a failed read-only step
    "partial": "partly applied, check the model",
}
# Failures that left the model as it was.
_UNCHANGED = frozenset({"rolled_back", "not_started", "not_applicable"})
# Error code -> short reason for the panel line; the log keeps the full message.
_SHORT_REASON = {
    "NX_OBJECT_STALE": "stale reference",
    "NX_NOT_FOUND": "object not found",
    "NX_OBJECT_OWNER_MISMATCH": "object belongs to another part",
    "NX_OBJECT_TYPE_MISMATCH": "wrong kind of object",
    "NX_INVALID_ARGUMENT": "invalid argument",
    "NX_UNSUPPORTED_ARGUMENT": "unsupported option",
    "NX_VERIFICATION_FAILED": "result failed verification",
    "NX_UPDATE_FAILED": "NX update failed",
    "NX_SKETCH_ACTIVE": "another sketch is active",
    "NX_NO_TARGET_BODY": "no target body",
    "NX_FILE_EXISTS": "file already exists",
    "NX_FILE_NOT_FOUND": "file not found",
    "NX_PATH_OUTSIDE_WORKSPACE": "path outside the workspace",
    "NX_UI_BUSY": "an NX dialog is open",
    "NX_ROLLBACK_FAILED": "undo failed",
    # Catalogue codes whose messages are long; others show their message.
    "NX_PART_NOT_GRANTED": "engineer's part, not granted",
    "NX_VERSION_UNSUPPORTED": "not tested on this NX version",
    "NX_RESTORE_FAILED": "view not fully restored",
}
_CREATED_KEYS = ("feature", "sketch", "component", "part", "drawing_sheet", "drawing_view", "body")
# Created kinds an engineer recognises, most meaningful first.
_MAIN_KINDS = ("sketch", "body", "component", "drawing_sheet", "drawing_view", "feature")


@dataclass
class Entry:
    started: float
    summary: str
    method: str = ""
    intent: str | None = None
    duration: float = 0.0
    ok: bool = True
    outcome: str | None = None
    reason: str | None = None
    code: str | None = None
    created: str | None = None
    inspections: int = 0  # >0: a group of read-only steps
    session: int = 0

    @property
    def changed_model(self) -> bool:
        return self.inspections == 0


def _clip(text: str, width: int = LINE_WIDTH) -> str:
    # Keep the double-space column gaps; only line breaks would break the layout.
    text = " ".join(str(text).splitlines())
    return text if len(text) <= width else text[: width - 1] + "…"


def created_name(result: dict[str, Any]) -> str | None:
    """Name of the main thing a successful step created, if the result says.

    NX creates helper objects along with the one the engineer asked for (a sketch
    also brings its sketch feature, datums and points), so name the step's own
    result, else the most meaningful created kind, never a list of internals.
    """
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    for key in _CREATED_KEYS:
        value = result.get(key) or data.get(key)  # legacy result or catalogue envelope
        if isinstance(value, dict) and value.get("name"):
            return str(value["name"])
    created = [
        c
        for c in (result.get("changes") or {}).get("created") or []
        if isinstance(c, dict) and c.get("name")
    ]
    if not created:
        return None
    own = result.get("object")
    if isinstance(own, dict) and any(c.get("id") == own.get("id") for c in created):
        return str(own.get("name") or created[0]["name"])
    for kind in _MAIN_KINDS:
        main = [c for c in created if c.get("kind") == kind]
        if main:
            return str(main[0]["name"]) if len(main) == 1 else f"{len(main)} {kind}s"
    kinds = {c.get("kind") for c in created}
    if len(created) > 1 and len(kinds) == 1 and None not in kinds:
        return f"{len(created)} {kinds.pop()}s"  # e.g. a rectangle's 4 curves
    return str(created[0]["name"])


@dataclass
class Activity:
    log_path: Path | None = None
    keep: int = 50
    entries: deque = field(default_factory=deque)
    session: int = 0
    _session_started: float | None = None

    def start_session(self) -> None:
        self.session += 1
        self._session_started = time.time()

    def record(
        self,
        method: str,
        summary: str,
        *,
        started: float,
        duration: float,
        intent: str | None = None,
        read_only: bool = False,
        result: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> Entry:
        code = None
        if error is None and isinstance(result, dict) and result.get("status") == "error":
            ok, outcome, reason = False, result.get("mutation_outcome"), result.get("message")
            code = result.get("code")
        elif error is not None:
            details = getattr(error, "details", None) or {}
            ok, outcome = False, details.get("mutation_outcome", "unknown")
            reason = getattr(error, "message", None) or str(error)
            code = getattr(error, "code", None)
        else:
            ok, outcome, reason = True, None, None
        entry = Entry(
            started=started,
            summary=summary,
            method=method,
            intent=intent,
            duration=duration,
            ok=ok,
            outcome=outcome,
            reason=reason,
            code=code,
            created=created_name(result) if ok and isinstance(result, dict) else None,
            session=self.session,
        )
        self._write(entry)
        last = self.entries[-1] if self.entries else None
        if read_only and ok:
            if last is not None and last.inspections and last.session == self.session:
                last.inspections += 1
                last.started = started
                return last
            entry.inspections = 1
        self.entries.append(entry)
        while len(self.entries) > self.keep:
            self.entries.popleft()
        return entry

    def line(self, entry: Entry) -> str:
        clock = time.strftime("%H:%M:%S", time.localtime(entry.started))
        if entry.inspections:
            steps = "step" if entry.inspections == 1 else "steps"
            return _clip(
                f"·  {clock}  {entry.inspections} read-only or view {steps} (model unchanged)"
            )
        # The summary gives way first: the result and the model state must stay visible.
        if entry.ok:
            made = f" → {_clip(entry.created, 24)}" if entry.created else ""
            tail = f"{made}  {entry.duration:.1f} s"
            head = f"✓  {clock}  "
            return head + _clip(entry.summary, LINE_WIDTH - len(head) - len(tail)) + tail
        state = _MODEL_STATE.get(entry.outcome or "", "outcome unknown, check the model")
        mark = "✗" if entry.outcome in _UNCHANGED else "⚠"
        head = f"{mark}  {clock}  "
        room = max(20, LINE_WIDTH - len(head) - len(state) - 3)
        line = f"{head}{_clip(entry.summary, room)} — {state}"
        reason = _SHORT_REASON.get(entry.code or "", entry.reason)
        if reason and len(line) + 4 < LINE_WIDTH:
            line += " " + _clip(f"({reason})", LINE_WIDTH - len(line) - 1)
        return line

    def recent(self, count: int = 4) -> list[str]:
        return [self.line(entry) for entry in list(self.entries)[-count:]]

    def session_recap(self) -> str | None:
        """One line on what the agent's latest control session did."""
        mine = [e for e in self.entries if e.session == self.session and e.changed_model]
        if not mine or self._session_started is None:
            return None
        changes = sum(1 for e in mine if e.ok)
        failed = len(mine) - changes
        span = (
            time.strftime("%H:%M", time.localtime(self._session_started))
            + "–"
            + time.strftime("%H:%M", time.localtime(mine[-1].started + mine[-1].duration))
        )
        plural = "change" if changes == 1 else "changes"
        tail = f", {failed} failed" if failed else ""
        return f"Agent's last session: {changes} {plural}{tail} ({span})"

    def _write(self, entry: Entry) -> None:
        """Append one full line to the activity log; never fail the operation."""
        if self.log_path is None:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.started))
        if entry.ok:
            made = f" → {entry.created}" if entry.created else ""
            text = f"{stamp}  OK      {entry.summary}{made}  ({entry.duration:.1f} s)"
        else:
            state = _MODEL_STATE.get(entry.outcome or "", "outcome unknown, check the model")
            code = f"{entry.code}: " if entry.code else ""
            text = f"{stamp}  FAILED  {entry.summary} — {state}: {code}{entry.reason}"
        text += f"  [{entry.method}]"
        if entry.intent:
            text += f"  note: “{entry.intent}”"
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_LIMIT_BYTES:
                self.log_path.replace(self.log_path.with_suffix(".log.1"))
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(" ".join(text.split("\n")) + "\n")
        except OSError:
            pass

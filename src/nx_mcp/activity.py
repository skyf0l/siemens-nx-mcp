"""Agent activity history for the NX MCP panel and the workspace activity log.

Standard library and ``nx_mcp.core`` only: the NX-side control panel imports this. Each entry says
what ran (the request summary), what it created, and what happened to the
model when it failed. Read-only steps are grouped so they do not push model
changes out of the short panel history. While two or more parts are loaded, every
step names the part file it acted on first (``[cover.prt] Extrude ...``). Engineer and
NX event lines name no part. ``touch``/``last_in`` keep the last agent step per part
file for the panel's "Parts in use" lines (``part_line``).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nx_mcp.core.errors import counted

LINE_WIDTH = 84
ENGINEER_CHANGE = "You changed the model — agent will re-read its references"
LOG_LIMIT_BYTES = 5 * 1024 * 1024
# Why a queued step never ran: its client gave up (``expires_at`` passed, or it closed
# its connection).
SKIPPED_REASON = "the agent stopped waiting"

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
    "NX_NOT_FOUND": "object not found",
    "NX_INVALID_ARGUMENT": "invalid argument",
    "NX_VERIFICATION_FAILED": "result failed verification",
    "NX_UPDATE_FAILED": "NX update failed",
    "NX_SKETCH_ACTIVE": "another sketch is active",
    "NX_FILE_NOT_FOUND": "file not found",
    "NX_PATH_OUTSIDE_WORKSPACE": "path outside the workspace",
    "NX_UI_BUSY": "an NX dialog is open",
    "NX_MODAL_DIALOG_OPEN": "an NX dialog waits for an answer",
    "NX_ROLLBACK_FAILED": "undo failed",
    "NX_CALL_EXPIRED": SKIPPED_REASON,
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
    note: str | None = None  # an engineer or NX event, not an agent step
    label: str = "ENGINEER"  # the log column of a note
    mark: str = "✎"  # the panel mark of a note
    part: str | None = None  # the target part's file (two or more parts loaded)
    detail: str | None = None  # a note's detail, written to the log only

    @property
    def shown(self) -> str:
        """The summary with the target part's file first, when known."""
        return with_part(self.part, self.summary)


def with_part(part: str | None, summary: str) -> str:
    """``[cover.prt] Extrude ...``: a step's line names its part when it is known."""
    return f"[{part}] {summary}" if part else summary


SHOWN = {"shown": "shown", "tab": "in a tab", "background": "background"}


def part_line(row: dict[str, Any]) -> str:
    """One "Parts in use" line: ``cover.prt  ·  shown  ·  modified  ·  12:03:04 Extrude``
    (``row``: file, shown, state, and last/at/ok of the last agent step, if any)."""
    head = f"{row.get('file')}  ·  {SHOWN.get(row.get('shown') or '', row.get('shown'))}"
    head += f"  ·  {row.get('state')}"
    last, at = row.get("last"), row.get("at")
    if not last or not isinstance(at, (int, float)):
        return _clip(head)
    clock = time.strftime("%H:%M:%S", time.localtime(at))
    failed = "" if row.get("ok", True) else " (failed)"
    return _clip(f"{head}  ·  {clock} {last}{failed}")


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
    data = result.get("data")
    if not isinstance(data, dict):
        data = {}
    for key in _CREATED_KEYS:
        value = result.get(key) or data.get(key)  # a development-tool result or catalogue envelope
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
            return str(main[0]["name"]) if len(main) == 1 else counted(len(main), kind)
    kinds = {c.get("kind") for c in created}
    if len(created) > 1 and len(kinds) == 1 and None not in kinds:
        return counted(len(created), str(kinds.pop()))  # e.g. a rectangle's 4 curves
    return str(created[0]["name"])


@dataclass
class Activity:
    log_path: Path | None = None
    keep: int = 50
    entries: deque = field(default_factory=deque)
    last_by_part: dict = field(default_factory=dict)  # part file -> its last agent step

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
        part: str | None = None,
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
            part=part,
        )
        self._write(entry)
        last = self.entries[-1] if self.entries else None
        if read_only and ok:
            if last is not None and last.inspections and last.part == part:
                last.inspections += 1
                last.started = started
                return last
            entry.inspections = 1
        self.entries.append(entry)
        while len(self.entries) > self.keep:
            self.entries.popleft()
        return entry

    def engineer_change(self, at: float | None = None, detail: str | None = None) -> Entry:
        """The engineer changed the model between two agent steps; ``detail`` (what
        changed: NX's undo marks, the loaded parts) goes to the log line. No part: the
        host cannot tell which part an engineer edit was in."""
        return self._note(
            Entry(
                started=time.time() if at is None else at,
                summary=ENGINEER_CHANGE,
                note=ENGINEER_CHANGE,
                detail=f"changed: {detail}" if detail else None,
            )
        )

    def touch(self, part: str | None, summary: str | None, *, started, ok: bool = True) -> None:
        """Remember an agent step as the last one in ``part`` (a file name; None: no
        part known, nothing kept)."""
        if part and summary:
            self.last_by_part[part.casefold()] = {"last": summary, "at": started, "ok": ok}

    def last_in(self, part: str) -> dict[str, Any]:
        """``{last, at, ok}`` of the last agent step in ``part``, or ``{}``."""
        return dict(self.last_by_part.get(str(part).casefold(), {}))

    def nx_dialog(self, text: str, at: float | None = None) -> Entry:
        """NX opened a modal dialog: NX and every call wait for a person to answer it."""
        return self._note(
            Entry(
                started=time.time() if at is None else at,
                summary=text,
                note=text,
                label="NX DIALOG",
                mark="⚠",
            )
        )

    def skipped(
        self,
        summary: str,
        reason: str = SKIPPED_REASON,
        *,
        method: str = "",
        at: float | None = None,
    ) -> Entry:
        """A queued agent step NX refused before it ran (its client gave up: the call
        expired or its connection closed): ``Skipped [b.prt] Extrude ...: <reason>``."""
        text = f"Skipped {summary}: {reason}"
        return self._note(
            Entry(
                started=time.time() if at is None else at,
                summary=summary,
                method=method,
                note=text,
                label="SKIPPED",
                mark="✗",
            )
        )

    def _note(self, entry: Entry) -> Entry:
        """Log and keep an event line (not an agent step's result)."""
        self._write(entry)
        self.entries.append(entry)
        while len(self.entries) > self.keep:
            self.entries.popleft()
        return entry

    def line(self, entry: Entry) -> str:
        clock = time.strftime("%H:%M:%S", time.localtime(entry.started))
        if entry.note:
            return _clip(f"{entry.mark}  {clock}  {entry.note}")
        if entry.inspections:
            steps = "step" if entry.inspections == 1 else "steps"
            where = f"[{entry.part}] " if entry.part else ""
            return _clip(
                f"·  {clock}  {where}{entry.inspections} read-only or view {steps} "
                "(model unchanged)"
            )
        # The summary gives way first: the result and the model state must stay visible.
        if entry.ok:
            made = f" → {_clip(entry.created, 24)}" if entry.created else ""
            tail = f"{made}  {entry.duration:.1f} s"
            head = f"✓  {clock}  "
            return head + _clip(entry.shown, LINE_WIDTH - len(head) - len(tail)) + tail
        state = _MODEL_STATE.get(entry.outcome or "", "outcome unknown, check the model")
        mark = "✗" if entry.outcome in _UNCHANGED else "⚠"
        head = f"{mark}  {clock}  "
        room = max(20, LINE_WIDTH - len(head) - len(state) - 3)
        line = f"{head}{_clip(entry.shown, room)} — {state}"
        reason = _SHORT_REASON.get(entry.code or "", entry.reason)
        if reason and len(line) + 4 < LINE_WIDTH:
            line += " " + _clip(f"({reason})", LINE_WIDTH - len(line) - 1)
        return line

    def recent(self, count: int = 4) -> list[str]:
        return [self.line(entry) for entry in list(self.entries)[-count:]]

    def _write(self, entry: Entry) -> None:
        """Append one full line to the activity log; never fail the operation."""
        if self.log_path is None:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.started))
        if entry.note:
            text = f"{stamp}  {entry.label}  {entry.note}"
            if entry.detail:
                text += f" ({entry.detail})"
        elif entry.ok:
            made = f" → {entry.created}" if entry.created else ""
            text = f"{stamp}  OK      {entry.shown}{made}  ({entry.duration:.1f} s)"
        else:
            state = _MODEL_STATE.get(entry.outcome or "", "outcome unknown, check the model")
            code = f"{entry.code}: " if entry.code else ""
            text = f"{stamp}  FAILED  {entry.shown} — {state}: {code}{entry.reason}"
        if entry.method:
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

"""Session status and operation receipts (tool-surface 3.1).

``nx_get_status`` replaces ``nx_status`` and ``nx_workspace_info``: NX build,
bridge state, panel mode (``auto`` or ``ask``: only the engineer changes it on the
NX MCP panel), work/display part, workspace rules and the running operation. It answers without waiting behind a running NX step: the interactive
host answers it on the bridge worker from its UI-thread snapshot while a step
runs (:func:`envelope_from_snapshot`). When NX's main thread is held by one long step
(it then accepts no connection), when the bridge does not answer within a few seconds
or when it is down, the sidecar answers itself (:func:`sidecar_envelope`) from the
host's heartbeat file and the NX process seen from outside (``host/heartbeat.py``):
bridge ``state`` busy, blocked_by_dialog, unresponsive, not_answering, down or unknown,
with ``bridge.health`` saying what NX runs, for how long, and what to do.

``nx_get_operation`` (replaces ``nx_operation_status``) and
``nx_cancel_operation`` run in the sidecar (``runs="sidecar"``): they read and
flag the shared receipt store (``core/receipts.py``, ``OperationStore``
records), so NX being busy or down never delays them. Cancel
writes the receipt's ``.cancel`` file, which ``tx.checkpoint`` honours between
steps (``nx_run_batch`` and other multi-step tools); one native call is never
interrupted.
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core import catalogue, target
from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Text
from nx_mcp.kit import files, parts, result

SECTIONS = ("bridge", "parts", "control", "workspace", "operation")
Section = Literal["bridge", "parts", "control", "workspace", "operation"]
# No schema pattern: the receipt store checks the id and says exactly what is wrong
# ("too short: 5 of at least 8 characters") instead of "has an invalid format".
OperationRef = Annotated[str, Text(160), Doc("operation_id of an earlier changing call")]
CancelRef = Annotated[str, Text(160), Doc("operation_id of the running call to cancel")]
# A running call whose receipt shows no progress for this long gets a note (a dialog, the
# engineer or one long native step may hold NX).
SILENT_SECONDS = 60.0
HEARTBEAT = (".nx-mcp", "ui-state.json")  # the interactive host's heartbeat (host/heartbeat.py)
# Why a receipt is missing: only calls that change something are receipted.
NO_RECEIPT = (
    "No receipt for this id: it was never used, or it belongs to a read-only call "
    "(reads such as nx_get_status are not receipted; only changing calls are). "
    "Do not infer failure."
)
RULES = {
    "path_host": "NX machine: paths refer to the NX host, not the client's filesystem",
    "relative_to": "workspace root",
    "absolute_paths": "accepted inside the workspace root only",
    "reserved": ".nx-mcp (internal state; never read or written by tools)",
    "current_directory": "none: pass explicit paths on every call",
}


MODE_LABELS = {"auto": "Auto", "ask": "Ask me first"}


class QueueData(TypedDict):
    waiting: int  # agent calls queued for NX (any reason: running call, card, busy engineer)
    oldest_seconds: float | None  # how long the oldest of them has waited


class ControlData(TypedDict):
    mode: str  # the NX MCP panel mode: auto or ask
    mode_label: str  # the panel's name for the mode: Auto or Ask me first
    busy: bool  # NX is not free: an agent call runs or waits, or the engineer is busy
    waiting: int  # agent calls waiting for NX now (queue.waiting)
    engineer_busy: bool  # the engineer is in an NX command or dialog
    waiting_for_answer: int  # of those, calls waiting for the engineer's answer (Ask me first)
    activity: str  # running, waiting_for_answer, waiting_for_engineer_command, queued or idle
    running: str | None  # the agent call running now
    queue: QueueData  # every agent call waiting for NX (status reads never wait)


def control_data(status: dict) -> ControlData:
    """The control section from the interactive host's status (or its snapshot).

    One count of waiting calls (``waiting`` = ``queue.waiting``, sampled when the status
    is read) and one ``busy`` that agrees with it, never ``busy: false, waiting: 0`` next
    to ``queue.waiting: 4`` (``busy`` once meant the engineer only and
    ``waiting`` the Ask me first card only; both are kept under their own names)."""
    mode = str(status.get("mode") or "")
    queue = _queue(status.get("queue"))
    engineer_busy = bool(status.get("engineer_busy"))
    answer = int(status.get("waiting_for_engineer") or 0)
    running = status.get("running_method") or None
    activity = str(status.get("activity") or "idle")
    if activity == "idle" and queue["waiting"]:
        activity = "queued"  # calls queued between two UI-thread ticks
    return {
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "busy": bool(running) or queue["waiting"] > 0 or engineer_busy or answer > 0,
        "waiting": max(queue["waiting"], answer),
        "engineer_busy": engineer_busy,
        "waiting_for_answer": answer,
        "activity": activity,
        "running": running,
        "queue": queue,
    }


def _queue(raw) -> QueueData:
    raw = raw if isinstance(raw, dict) else {}
    oldest = raw.get("oldest_seconds")
    return {
        "waiting": int(raw.get("waiting") or 0),
        "oldest_seconds": float(oldest) if isinstance(oldest, (int, float)) else None,
    }


class HealthRunning(TypedDict):
    tool: str
    part: str | None  # the target part's file
    operation_id: str | None  # only when the status call passes that operation_id
    seconds: int | None  # how long it has run
    since: str | None  # UTC start time


class HealthInfo(TypedDict):
    state: str  # same as bridge.state
    pid: int | None  # the NX process
    process: dict  # {alive, responding, cpu_busy}: seen from outside NX (None: unknown)
    running: HealthRunning | None  # the agent call NX runs, from the host's heartbeat
    heartbeat_age_seconds: float | None  # since the host last wrote its heartbeat
    dialogs: list[dict]  # modal NX dialogs waiting for the engineer
    message: str
    hint: str  # what to do now


class _BridgeInfo(TypedDict):
    # connected; else (answered by the sidecar): busy, blocked_by_dialog, unresponsive,
    # not_answering, down or unknown
    state: str
    nx_version: str | None  # build key, e.g. v2406 (evidence gates by it)
    nx_build: str | None  # NX's own full build, e.g. 2406.9160
    build_tested: bool  # NX MCP has acceptance evidence for this build
    interactive: bool  # NX with the NX MCP panel
    session_id: str | None
    answered_from: str  # nx, snapshot (NX is running a step) or sidecar (bridge down)
    # Tested tools/variants NX refused for a missing license in the last 15 minutes:
    # {tool, variant, nx_code, since, message}; hidden from discovery.
    license_unavailable: list[dict]


class BridgeInfo(_BridgeInfo, total=False):
    health: HealthInfo  # only when answered_from is sidecar: why NX did not answer
    # Only when not empty: tools that failed on this untested build because NX lacks an
    # NXOpen member, {tool, variant, build, member, since}; refused and hidden this session.
    version_unsupported: list[dict]
    # Untested builds only: NX's undo self-test before the first change, {state (passed,
    # failed, skipped), reason, at, seconds}; None before it ran.
    self_test: dict | None
    # Untested builds only, when agents may only read and view: why.
    read_only: str


class PartInfo(TypedDict):
    part: result.Row | None  # None when answered from the snapshot
    path: str
    units: str | None
    modified: bool


class PartsInfo(TypedDict, total=False):
    work: PartInfo | None
    display: PartInfo | None
    loaded: int | None
    # the component the work part is edited through in the displayed assembly (WAVE:
    # display = assembly, work = that component's part), else None
    work_component: dict | None
    top_level: int  # loaded parts no loaded assembly uses as a component
    # gate 3t: the agent's loaded parts (created, opened or granted; components
    # included, never the engineer's own); from two on, a call without refs takes part
    agent_loaded: int


class WorkspaceInfo(TypedDict):
    root: str
    path_host: str
    relative_to: str
    absolute_paths: str
    reserved: str
    current_directory: str


class RunningInfo(TypedDict):
    tool: str
    # null unless this status call passes the running call's own operation_id: another
    # agent's call is never named to a reader
    operation_id: str | None
    elapsed_seconds: float | None
    progress: dict | None  # {completed, total} from the receipt


class StatusData(TypedDict, total=False):
    bridge: BridgeInfo
    parts: PartsInfo | None
    control: ControlData | None  # None without the NX MCP panel
    workspace: WorkspaceInfo
    operation: RunningInfo | None  # the call NX is running now


class OperationData(TypedDict, total=False):
    operation_id: str
    # running, committed, reverted (committed, then an undo or restore reverted it: its
    # result's refs are stale), failed, unknown
    state: str
    tool: str | None
    mutation_outcome: str | None
    started_at: str | None
    finished_at: str | None
    progress: dict | None  # {completed, total, at, age_seconds}
    # a running call NX cannot go on with: {title, seconds, since} of the modal dialog
    blocked_by_dialog: dict | None
    note: str | None  # why a running call shows no progress
    cancel_requested: bool
    reverted_by: str | None  # the undo (operation_id) or checkpoint that reverted it
    # the run an undo reverted before this one ran again under the same id (re-run):
    # {started_at, finished_at, reverted_by, created, earlier}
    previous: dict | None
    reason: str | None  # why the state is unknown
    error: dict | None
    result: dict | None  # the committed call's envelope (with full), else None
    result_stale: bool  # true: reverted, the result's refs are stale
    arguments: dict | None  # the request, file contents left out (with full)
    omitted: list[str]  # what full would add: result, arguments


class CancelData(TypedDict):
    operation_id: str
    tool: str | None
    state: str
    cancel_requested: bool
    note: str  # the call stops at its next step; nx_get_operation shows its outcome


# ---------------------------------------------------------------------------
# status


def workspace_info(root) -> WorkspaceInfo:
    return {"root": str(root), **RULES}  # type: ignore[typeddict-item]  # RULES holds the other fields


def _part(session_part, root, row=None, units=None) -> PartInfo | None:
    if session_part is None:
        return None
    full = parts.file_of(session_part)
    return {
        "part": row,
        "path": files.relative(full, root) if full else "",
        "units": units,
        "modified": bool(getattr(session_part, "IsModified", False)),
    }


def snapshot_parts(session, root) -> PartsInfo | None:
    """Work/display part for the host's UI-thread snapshot (no references made)."""
    try:
        session_parts = session.Parts
        return {
            "work": _part(session_parts.Work, root),
            "display": _part(session_parts.Display, root),
            "loaded": len(list(session_parts)),
        }
    except Exception:
        return None


def _shared(parts) -> bool:
    """Whether a snapshot shows two or more loaded parts (unknown: shared)."""
    try:
        return int(parts.get("agent_loaded", parts["loaded"])) >= 2
    except Exception:
        return True


def running_info(
    status: dict | None, store=None, reader=None, *, shared: bool = False
) -> RunningInfo | None:
    """The call the host is running, from its status or snapshot. With two or more parts
    loaded (``shared``) its ``operation_id`` shows only to a ``reader`` (the status call's
    own operation_id) that names it: a reader never learns another agent's id (to query
    or cancel it); solo, it shows as before. The tool, elapsed time and progress show to
    every reader."""
    if not status or not status.get("running_method"):
        return None
    operation_id = status.get("running_operation_id")
    progress = None
    if store is not None and operation_id:
        with contextlib.suppress(Exception):
            progress = store.get(operation_id).get("progress")
    elapsed = status.get("operation_elapsed_seconds")
    started = status.get("_operation_monotonic")
    if started is not None:
        elapsed = time.monotonic() - started
    return {
        "tool": str(status["running_method"]),
        "operation_id": operation_id
        if operation_id and (reader == operation_id or not shared)
        else None,
        "elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
        "progress": progress,
    }


def _bridge(build, *, state, interactive, session_id, answered_from) -> BridgeInfo:
    from nx_mcp.core import evidence
    from nx_mcp.core.evidence import build_key, licenses

    nx = answered_from != "sidecar"  # the session memory lives in NX
    tested = evidence.known_build(build_key(build))
    out: BridgeInfo = {
        "state": state,
        "nx_version": build_key(build),
        "nx_build": str(build) if build else None,
        "build_tested": tested,
        "interactive": interactive,
        "session_id": session_id,
        "answered_from": answered_from,
        "license_unavailable": licenses() if nx else [],
    }
    failed = evidence.version_failures() if nx else []
    if failed:
        out["version_unsupported"] = failed
    if not tested and nx:
        out["self_test"] = evidence.self_test_result()
        reason = read_only_reason()
        if reason:
            out["read_only"] = reason
    return out


def read_only_reason() -> str | None:
    """Why agents may only read and view on this untested NX build, else None."""
    from nx_mcp.core import evidence

    if evidence.untested_mode() == "read_only":
        return "NX_MCP_UNTESTED=read_only"
    result = evidence.self_test_result()
    if result is not None and result.get("state") == "failed":
        return f"NX's undo self-test failed: {result.get('reason') or 'no reason given'}"
    return None


def _select(data: dict, include) -> dict:
    wanted = set(include or SECTIONS)
    return {k: v for k, v in data.items() if k in wanted}


def _envelope(data: dict, params: dict, warnings=()) -> dict:
    return {
        "tool": "nx_get_status",
        "operation_id": params.get("operation_id"),
        "ok": True,
        "mutation_outcome": "not_applicable",
        "frame": None,
        "data": _select(data, params.get("include")),
        "warnings": list(warnings),
    }


def envelope_from_snapshot(snapshot: dict, params: dict, *, root, build, store=None) -> dict:
    """The status while NX runs a step: the host's UI-thread snapshot, on the worker."""
    data = {
        "bridge": _bridge(
            build, state="connected", interactive=True, session_id=None, answered_from="snapshot"
        ),  # fmt: skip
        "parts": snapshot.get("parts"),
        "control": control_data(snapshot),
        "workspace": workspace_info(root),
        "operation": running_info(
            snapshot, store, params.get("operation_id"), shared=_shared(snapshot.get("parts"))
        ),
    }
    return _envelope(data, params)


def sidecar_envelope(
    root, health: dict, params: dict, *, build=None, beat: dict | None = None, error=None
) -> dict:
    """The status the sidecar gives when NX's bridge cannot answer now: ``health`` from
    ``host.heartbeat.assess``; ``beat`` the host's heartbeat (its last status), which
    gives the control section; ``error`` the bridge error, when there was one."""
    bridge = _bridge(
        build,
        state=str(health.get("state") or "unknown"),
        interactive=bool(beat and beat.get("interactive")),
        session_id=(beat or {}).get("session_id"),
        answered_from="sidecar",
    )
    bridge["health"] = health  # type: ignore[typeddict-item]
    call = health.get("running")
    operation: RunningInfo | None = None
    if call:
        operation = {
            "tool": call["tool"],
            "operation_id": call.get("operation_id"),
            "elapsed_seconds": call.get("seconds"),
            "progress": None,
        }
    data = {
        "bridge": bridge,
        "parts": None,
        "control": control_data(beat) if beat and health.get("state") != "down" else None,
        "workspace": workspace_info(root),
        "operation": operation,
    }
    message = str(health.get("message") or "NX did not answer")
    if error and health.get("state") in ("down", "unknown", "not_answering"):
        message += f" ({error.get('message') or error.get('code')})"
    return _envelope(data, params, [{"code": "NX_UPDATE_WARNING", "message": message}])


def _work_component(tx: Tx, session_parts) -> dict | None:
    """The work component (``Parts.WorkComponent``) when the work part is a component
    part of the displayed assembly, else None."""
    try:
        component = getattr(session_parts, "WorkComponent", None)
        work, shown = session_parts.Work, session_parts.Display
        if component is None or work is None or shown is None or work.Tag == shown.Tag:
            return None
        return dict(tx.ref(component, "component"))
    except Exception:  # a status read never fails on this detail
        return None


@tool(
    effect="read",
    label="Get NX status",
    summary="Check NX, bridge and workspace status",
    keywords=(
        "status",
        "nx version",
        "bridge health",
        "work part",
        "workspace root",
        "panel mode",
        "ask me first",
        "is nx busy",
    ),  # fmt: skip
    cost="light",
    example={"include": ["bridge", "parts"]},
)
def nx_get_status(
    tx: Tx,
    *,
    include: Annotated[list[Section], Items(1, 5), Doc("Sections to return (default all)")]
    | None = None,
) -> StatusData:
    """NX build, bridge state, panel mode, work/display part, workspace root/rules, running
    operation. Answers in seconds even when NX is busy, hung or down (bridge.health then says
    what runs and what to do). Mode auto or ask: only the engineer changes it."""
    services = tx.services
    status = services.control_status()
    session_parts = services.session.Parts
    root = services.workspace.root

    def row(part):
        if part is None:
            return None
        return _part(part, root, tx.ref(part, "part"), services.part_units(part))

    data = {
        "bridge": _bridge(
            services.build,
            state="connected",
            interactive=status is not None,
            session_id=services.session_id,
            answered_from="nx",
        ),  # fmt: skip
        "parts": {
            "work": row(session_parts.Work),
            "display": row(session_parts.Display),
            "loaded": len(services.parts()),
            "work_component": _work_component(tx, session_parts),
            "top_level": len(target.roots(services)),
            "agent_loaded": len(target.agent_parts(services)),
        },
        "control": control_data(status) if status is not None else None,
        "workspace": workspace_info(root),
        "operation": running_info(
            status, services.store, tx.operation_id, shared=len(target.agent_parts(services)) >= 2
        ),
    }
    return _select(data, include)  # type: ignore[return-value]  # the fields include picks


# ---------------------------------------------------------------------------
# operations


FINISHED_HINTS = {
    "committed": "Nothing to cancel; to revert it use nx_undo_operation",
    "failed": "Nothing to cancel; nx_get_operation shows its error and mutation_outcome",
    "unknown": "Nothing is running; check its outcome with nx_get_operation",
}


def _cancel_flag(store, operation_id: str) -> bool:
    try:
        return store.path(operation_id).with_suffix(".cancel").exists()
    except Exception:
        return False


def _record(tx: Tx, operation: str) -> dict:
    try:
        record = tx.services.store.get(operation)
    except Exception as error:
        if getattr(error, "code", None) == "NX_INVALID_ARGUMENT":
            message = str(getattr(error, "message", error)).replace("operation_id", "operation", 1)
            fail("NX_INVALID_ARGUMENT", message, field="/operation",
                 hint="Pass the operation_id an earlier call returned, e.g. "
                 "'op_0123456789abcdef'")  # fmt: skip
        fail("NX_ERROR", f"The receipt cannot be read: {error}", field="/operation")
    if record.get("state", "unknown") == "unknown" and "fingerprint" not in record:
        record = {**record, "reason": NO_RECEIPT}
    return record


def _iso(seconds) -> str | None:
    if not isinstance(seconds, (int, float)):
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dialog_row(blocked: dict | None, now: float | None = None) -> dict | None:
    """A modal dialog that blocks NX as status reads show it: title and text, ``since``
    as UTC ISO 8601 and ``seconds`` it has waited (the host's window handle and its raw
    epoch stay inside the host)."""
    if not isinstance(blocked, dict):
        return None
    since = blocked.get("since")
    now = time.time() if now is None else now
    row = {k: v for k, v in blocked.items() if k not in ("hwnd", "since")}
    row["since"] = _iso(since)
    row["seconds"] = round(now - since) if isinstance(since, (int, float)) else None
    return row


def _heartbeat(root) -> dict | None:
    """The interactive host's last heartbeat (its status), None when absent or unreadable."""
    try:
        data = json.loads(Path(root, *HEARTBEAT).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _age(at) -> float | None:
    try:
        return max(0.0, time.time() - datetime.fromisoformat(str(at)).timestamp())
    except (TypeError, ValueError):
        return None


def _running(tx: Tx, operation: str, record: dict, out: dict) -> None:
    """A running call: how old its last progress is, and the modal dialog NX waits on when
    the host's heartbeat says this call is the one it blocks."""
    progress = record.get("progress")
    since = progress.get("at") if isinstance(progress, dict) else record.get("started_at")
    age = _age(since)
    if isinstance(progress, dict) and age is not None:
        out["progress"] = {**progress, "age_seconds": round(age)}
    beat = None
    with contextlib.suppress(Exception):
        beat = _heartbeat(tx.workspace.root)
    blocked = (beat or {}).get("blocked_by_dialog")
    if isinstance(blocked, dict) and (beat or {}).get("running_operation_id") == operation:
        out["blocked_by_dialog"] = dialog_row(blocked)
        out["note"] = (
            "NX waits for the engineer to answer a modal dialog; the call goes on (or "
            "fails) once it is answered. The agent never clicks NX dialogs"
        )
    elif age is not None and age > SILENT_SECONDS:
        out["note"] = (
            f"No progress for {round(age)} s: one long native step, a modal dialog or the "
            "engineer may hold NX; nx_get_status says which"
        )


@tool(
    effect="read",
    label="Get operation",
    summary="Check operation {operation}",
    keywords=(
        "operation status",
        "receipt",
        "did it commit",
        "outcome unknown",
        "batch progress",
        "retry safely",
        "was it undone",
    ),  # fmt: skip
    cost="light",
    runs="sidecar",
    example={"operation": "op_0123456789abcdef"},
)
def nx_get_operation(
    tx: Tx,
    *,
    operation: OperationRef,
    full: Annotated[bool, Doc("Also return the arguments and result")] = False,
) -> OperationData:
    """Durable state of an earlier changing call by its operation_id: running, committed,
    reverted (undone), failed or unknown. Answers while NX is busy or down; unknown never
    authorizes a blind retry."""
    operation = operation.strip()
    record = _record(tx, operation)
    state = str(record.get("state") or "unknown")
    reverted = state == "committed" and bool(record.get("reverted_by"))
    stored = record.get("envelope") if isinstance(record.get("envelope"), dict) else None
    error = record.get("error")
    if isinstance(error, dict):
        error = {k: error[k] for k in ("code", "category", "message", "field") if k in error}
    out: OperationData = {
        "operation_id": operation,
        "state": "reverted" if reverted else state,
        "tool": record.get("tool"),
        "mutation_outcome": record.get("mutation_outcome"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "progress": record.get("progress"),
        "cancel_requested": _cancel_flag(tx.services.store, operation),
        "reverted_by": record.get("reverted_by"),
        "previous": record.get("previous"),
        "reason": record.get("reason"),
        "error": error if state != "committed" else None,
        "result": None,
        "arguments": None,
    }
    if state == "running":
        _running(tx, operation, record, out)  # type: ignore[arg-type]
    omitted = []
    if state == "committed" and stored is not None:
        if full:
            out["result"] = stored
            if reverted:
                out["result_stale"] = True
        else:
            omitted.append("result")
    if record.get("arguments") is not None:
        if full:
            out["arguments"] = record.get("arguments")
        else:
            omitted.append("arguments")
    if omitted:
        out["omitted"] = omitted
    return out


@tool(
    effect="session",
    label="Cancel operation",
    summary="Cancel operation {operation}",
    keywords=("cancel", "stop batch", "abort operation", "interrupt", "stop running change"),
    cost="light",
    runs="sidecar",
    example={"operation": "op_0123456789abcdef"},
)
def nx_cancel_operation(tx: Tx, *, operation: CancelRef) -> CancelData:
    """Ask a running changing call (nx_run_batch or another multi-step tool) to stop at its
    next step; it then rolls back. Only changing calls have receipts: reads (checks,
    measurements) cannot be cancelled. One native NX call is never interrupted; check the
    outcome with nx_get_operation."""
    store = tx.services.store
    operation = operation.strip()
    with tx.checks():
        record = _record(tx, operation)
        state = str(record.get("state") or "unknown")
        name = record.get("tool")
        if state == "unknown" and "fingerprint" not in record:
            fail("NX_NOT_FOUND",
                 f"No receipt for operation {operation}: only changing calls have receipts "
                 "and can be cancelled; a read (nx_check_model, a measurement) cannot",
                 field="/operation",
                 hint="Pass the operation_id you gave a changing call that is still running "
                 "(nx_get_status shows which tool runs)")  # fmt: skip
        if state != "running":
            # Not a blocker that clears (NX_IN_USE): cancelling a finished call never works.
            fail("NX_NOT_RUNNING",
                 f"Operation {operation} ({name}) already finished as {state}; only a "
                 "running call can be cancelled", field="/operation",
                 hint=FINISHED_HINTS.get(state, FINISHED_HINTS["unknown"]),
                 details={"state": state, "tool": name})  # fmt: skip
        if not isinstance(name, str) or catalogue.get(name) is None:
            fail("NX_UNSUPPORTED_OPTION", f"{name} does not support cancellation",
                 field="/operation", details={"tool": name})  # fmt: skip
    tx.touched = True
    store.path(operation).with_suffix(".cancel").touch()
    tx.outcome = "not_applicable"  # a request flag: the call itself decides at its next step
    return {
        "operation_id": operation,
        "tool": name,
        "state": state,
        "cancel_requested": True,
        "note": "The call stops at its next step and rolls back; nx_get_operation shows "
        "its outcome",
    }

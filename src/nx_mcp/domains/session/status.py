"""Session status and operation receipts (tool-surface 3.1).

``nx_get_status`` replaces ``nx_status`` and ``nx_workspace_info``: NX build,
bridge state, panel mode (``auto`` or ``ask``: only the engineer changes it on the
NX MCP panel), work/display part, workspace rules and the running operation. It answers without waiting behind a running NX step: the interactive
host answers it on the bridge worker from its UI-thread snapshot while a step
runs (:func:`envelope_from_snapshot`), and the sidecar answers it itself when the
bridge is down (:func:`offline_envelope`, bridge ``unavailable``).

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
import time
from typing import Annotated, Literal, TypedDict

from nx_mcp.core import catalogue
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


class ControlData(TypedDict):
    mode: str  # the NX MCP panel mode: auto or ask
    mode_label: str  # the panel's name for the mode: Auto or Ask me first
    busy: bool  # the engineer is in an NX command or dialog
    waiting: int  # agent calls waiting for the engineer's answer (Ask me first)
    activity: str  # running, waiting_for_answer, waiting_for_engineer_command or idle
    running: str | None  # the agent call running now


def control_data(status: dict) -> ControlData:
    """The control section from the interactive host's status (or its snapshot)."""
    mode = str(status.get("mode") or "")
    return {
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "busy": bool(status.get("engineer_busy")),
        "waiting": int(status.get("waiting_for_engineer") or 0),
        "activity": str(status.get("activity") or "idle"),
        "running": status.get("running_method") or None,
    }


class BridgeInfo(TypedDict):
    state: str  # connected or unavailable
    nx_version: str | None  # build key, e.g. v2406
    nx_build: str | None  # NX's own version text
    interactive: bool  # NX with the NX MCP panel
    session_id: str | None
    answered_from: str  # nx, snapshot (NX is running a step) or sidecar (bridge down)
    # Tested tools/variants NX refused for a missing license this session (until restart):
    # {tool, variant, nx_code, since, message}; hidden from discovery.
    license_unavailable: list[dict]


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


class WorkspaceInfo(TypedDict):
    root: str
    path_host: str
    relative_to: str
    absolute_paths: str
    reserved: str
    current_directory: str


class RunningInfo(TypedDict):
    tool: str
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
    state: str  # running, committed, failed, unknown
    tool: str | None
    mutation_outcome: str | None
    started_at: str | None
    finished_at: str | None
    progress: dict | None  # {completed, total, at}
    cancel_requested: bool
    reverted_by: str | None
    reason: str | None  # why the state is unknown
    error: dict | None
    result: dict | None  # the committed call's envelope
    arguments: dict | None  # the request (file contents omitted)


class CancelData(TypedDict):
    operation_id: str
    tool: str | None
    state: str
    cancel_requested: bool


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


def running_info(status: dict | None, store=None) -> RunningInfo | None:
    """The call the host is running, from its status or snapshot."""
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
        "operation_id": operation_id,
        "elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
        "progress": progress,
    }


def _bridge(build, *, state, interactive, session_id, answered_from) -> BridgeInfo:
    from nx_mcp.core.evidence import build_key, licenses

    return {
        "state": state,
        "nx_version": build_key(build),
        "nx_build": str(build) if build else None,
        "interactive": interactive,
        "session_id": session_id,
        "answered_from": answered_from,
        "license_unavailable": licenses() if answered_from != "sidecar" else [],
    }


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
        "operation": running_info(snapshot, store),
    }
    return _envelope(data, params)


def offline_envelope(root, error: dict, params: dict, *, build=None) -> dict:
    """The status the sidecar gives when the bridge does not answer."""
    data = {
        "bridge": _bridge(
            build, state="unavailable", interactive=False, session_id=None, answered_from="sidecar"
        ),  # fmt: skip
        "parts": None,
        "control": None,
        "workspace": workspace_info(root),
        "operation": None,
    }
    message = f"NX bridge unavailable: {error.get('message') or error.get('code')}"
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
    """NX build, bridge state, panel mode, work and display part, workspace root and rules.
    Also the running operation; answers while NX runs a step (from a snapshot) and
    when the bridge is down. Panel mode: auto or ask (Ask me first); only the engineer
    changes it."""
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
        },
        "control": control_data(status) if status is not None else None,
        "workspace": workspace_info(root),
        "operation": running_info(status, services.store),
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
    ),  # fmt: skip
    cost="light",
    runs="sidecar",
    example={"operation": "op_0123456789abcdef"},
)
def nx_get_operation(tx: Tx, *, operation: OperationRef) -> OperationData:
    """Durable state of an earlier changing call by its operation_id: running (with
    progress), committed, failed or unknown. Answers while NX is busy or down; unknown
    never authorizes a blind retry."""
    operation = operation.strip()
    record = _record(tx, operation)
    state = str(record.get("state") or "unknown")
    stored = record.get("envelope") if isinstance(record.get("envelope"), dict) else None
    error = record.get("error")
    if isinstance(error, dict):
        error = {k: error[k] for k in ("code", "category", "message", "field") if k in error}
    return {
        "operation_id": operation,
        "state": state,
        "tool": record.get("tool"),
        "mutation_outcome": record.get("mutation_outcome"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "progress": record.get("progress"),
        "cancel_requested": _cancel_flag(tx.services.store, operation),
        "reverted_by": record.get("reverted_by"),
        "reason": record.get("reason"),
        "error": error if state != "committed" else None,
        "result": stored if state == "committed" else None,
        "arguments": record.get("arguments"),
    }


@tool(
    effect="session",
    label="Cancel operation",
    summary="Cancel operation {operation}",
    keywords=("cancel", "stop batch", "abort operation", "interrupt", "stop running call"),
    cost="light",
    runs="sidecar",
    example={"operation": "op_0123456789abcdef"},
)
def nx_cancel_operation(tx: Tx, *, operation: CancelRef) -> CancelData:
    """Ask a running call (nx_run_batch or another multi-step tool) to stop at its next
    step; it then rolls back. One native NX call is never interrupted; check the
    outcome with nx_get_operation."""
    store = tx.services.store
    operation = operation.strip()
    with tx.checks():
        record = _record(tx, operation)
        state = str(record.get("state") or "unknown")
        name = record.get("tool")
        if state == "unknown" and "fingerprint" not in record:
            fail("NX_NOT_FOUND", f"No receipt for operation {operation}", field="/operation",
                 hint="Pass the operation_id of a changing call that is still running "
                 "(nx_get_status shows it under operation); reads are not receipted")  # fmt: skip
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
    return {"operation_id": operation, "tool": name, "state": state, "cancel_requested": True}

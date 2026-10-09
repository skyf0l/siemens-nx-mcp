"""Session status and operation receipts (tool-surface 3.1).

``nx_get_status`` replaces ``nx_status`` and ``nx_workspace_info``: NX build,
bridge state, panel mode, work/display part, workspace rules and the running
operation. It answers without waiting behind a running NX step: the interactive
host answers it on the bridge worker from its UI-thread snapshot while a step
runs (:func:`envelope_from_snapshot`), and the sidecar answers it itself when the
bridge is down (:func:`offline_envelope`, bridge ``unavailable``).

``nx_get_operation`` (replaces ``nx_operation_status``) and
``nx_cancel_operation`` run in the sidecar (``runs="sidecar"``): they read and
flag the shared receipt store (``core/receipts.py`` and the legacy
``OperationStore`` records), so NX being busy or down never delays them. Cancel
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
from nx_mcp.domains.session.control import ControlData, control_data
from nx_mcp.kit import files, parts, result

SECTIONS = ("bridge", "parts", "control", "workspace", "operation")
Section = Literal["bridge", "parts", "control", "workspace", "operation"]
OperationRef = Annotated[
    str,
    Text(128, r"^[A-Za-z0-9_-]{8,128}$"),
    Doc("The operation_id of the call to look up"),
]
RULES = {
    "path_host": "NX machine: paths refer to the NX host, not the client's filesystem",
    "relative_to": "workspace root",
    "absolute_paths": "accepted inside the workspace root only",
    "reserved": ".nx-mcp (internal state; never read or written by tools)",
    "current_directory": "none: pass explicit paths on every call",
}


class BridgeInfo(TypedDict):
    state: str  # connected or unavailable
    nx_version: str | None  # build key, e.g. v2406
    nx_build: str | None  # NX's own version text
    interactive: bool  # NX with the NX MCP panel
    session_id: str | None
    answered_from: str  # nx, snapshot (NX is running a step) or sidecar (bridge down)


class PartInfo(TypedDict):
    part: result.Row | None  # None when answered from the snapshot
    path: str
    units: str | None
    modified: bool


class PartsInfo(TypedDict):
    work: PartInfo | None
    display: PartInfo | None
    loaded: int | None


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
    result: dict | None  # the committed result (envelope or legacy payload)


class CancelData(TypedDict):
    operation_id: str
    tool: str | None
    state: str
    cancel_requested: bool


# ---------------------------------------------------------------------------
# status


def workspace_info(root) -> WorkspaceInfo:
    return {"root": str(root), **RULES}  # type: ignore[typeddict-item]


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
    from nx_mcp.core.evidence import build_key

    return {
        "state": state,
        "nx_version": build_key(build),
        "nx_build": str(build) if build else None,
        "interactive": interactive,
        "session_id": session_id,
        "answered_from": answered_from,
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
        "is nx busy",
    ),  # fmt: skip
    cost="instant",
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
    when the bridge is down. Panel modes: auto, agent (Agent drives), ask."""
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
        },
        "control": control_data(status) if status is not None else None,
        "workspace": workspace_info(root),
        "operation": running_info(status, services.store),
    }
    return _select(data, include)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# operations


def _cancel_flag(store, operation_id: str) -> bool:
    try:
        return store.path(operation_id).with_suffix(".cancel").exists()
    except Exception:
        return False


def _record(tx: Tx, operation: str) -> dict:
    try:
        return tx.services.store.get(operation)
    except Exception as error:
        code = getattr(error, "code", None)
        if code == "NX_INVALID_ARGUMENT":
            fail("NX_INVALID_ARGUMENT", "operation: 8-128 letters, digits, _ or -",
                 field="/operation")  # fmt: skip
        fail("NX_ERROR", f"The receipt cannot be read: {error}", field="/operation")


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
    cost="instant",
    runs="sidecar",
    example={"operation": "op_0123456789abcdef"},
)
def nx_get_operation(tx: Tx, *, operation: OperationRef) -> OperationData:
    """Durable state of an earlier call by its operation_id: running (with progress),
    committed, failed or unknown. Answers while NX is busy or down; unknown never
    authorizes a blind retry."""
    record = _record(tx, operation)
    state = str(record.get("state") or "unknown")
    stored = record.get("envelope") if isinstance(record.get("envelope"), dict) else None
    error = record.get("error")
    if isinstance(error, dict):
        error = {k: error[k] for k in ("code", "category", "message", "field") if k in error}
    return {
        "operation_id": operation,
        "state": state,
        "tool": record.get("tool") or record.get("method"),
        "mutation_outcome": record.get("mutation_outcome"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "progress": record.get("progress"),
        "cancel_requested": _cancel_flag(tx.services.store, operation),
        "reverted_by": record.get("reverted_by"),
        "reason": record.get("reason"),
        "error": error if state != "committed" else None,
        "result": (stored or record.get("result")) if state == "committed" else None,
    }


@tool(
    effect="session",
    label="Cancel operation",
    summary="Cancel operation {operation}",
    keywords=("cancel", "stop batch", "abort operation", "interrupt", "stop running call"),
    cost="instant",
    runs="sidecar",
    example={"operation": "op_0123456789abcdef"},
)
def nx_cancel_operation(tx: Tx, *, operation: OperationRef) -> CancelData:
    """Ask a running call (nx_run_batch or another multi-step tool) to stop at its next
    step; it then rolls back. One native NX call is never interrupted; check the
    outcome with nx_get_operation."""
    store = tx.services.store
    with tx.checks():
        record = _record(tx, operation)
        state = str(record.get("state") or "unknown")
        name = record.get("tool") or record.get("method")
        if state == "unknown" and "fingerprint" not in record:
            fail("NX_NOT_FOUND", f"No receipt for operation {operation}", field="/operation",
                 hint="Check the id; nx_get_operation shows its state")  # fmt: skip
        if state != "running":
            fail("NX_IN_USE", f"Operation {operation} is {state}; only a running call can be "
                 "cancelled", field="/operation", details={"state": state, "tool": name})  # fmt: skip
        if catalogue.get(name) is None:
            fail("NX_UNSUPPORTED_OPTION", f"{name} does not support cancellation",
                 field="/operation", details={"tool": name})  # fmt: skip
    tx.touched = True
    store.path(operation).with_suffix(".cancel").touch()
    return {"operation_id": operation, "tool": name, "state": state, "cancel_requested": True}

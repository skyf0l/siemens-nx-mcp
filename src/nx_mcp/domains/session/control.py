"""``nx_set_control``: read or change who drives NX through the NX MCP panel.

It replaces ``nx_ui_control`` (tool-surface 3.1). The panel has three modes
(INTERACTIVE-NX.md): ``auto`` (the engineer works in NX; agent steps run between
their commands), ``agent`` ("Agent drives": NX stays locked for the agent) and
``ask`` ("Ask me first": each step waits for the engineer's OK). ``manual`` is the
old name of ``ask``.

The agent may lower its own control at once (agent > auto > ask). A request for
more control never reaches this handler before the engineer answered the request
card on the panel: the interactive host holds it (up to 60 s, then
``NX_CONTROL_PAUSED``; "Not now" gives ``NX_CONTROL_DECLINED``). A raise that
reaches the services without that approval is refused, nothing changed.

``status`` reads the mode without queueing behind native work: the interactive
host answers it on the bridge worker from its UI-thread snapshot
(:func:`envelope_from_snapshot`) and only falls back to this handler without one.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx

MODES = ("auto", "agent", "ask")
LABELS = {"auto": "Auto", "agent": "Agent drives", "ask": "Ask me first"}
ALIASES = {"manual": "ask"}


class ControlData(TypedDict):
    mode: str  # auto, agent or ask
    mode_label: str  # the panel's name for the mode
    owner: Literal["agent", "engineer"]  # who has NX input now
    busy: bool  # the engineer is in an NX command
    waiting: int  # agent calls waiting for the engineer's answer
    activity: str  # running, waiting_for_answer, waiting_for_engineer_command, idle, ...
    running: str | None  # the agent call running now


def control_data(status: dict) -> ControlData:
    """The tool's data from the interactive host's status (or its snapshot)."""
    mode = str(status.get("mode") or "")
    return {
        "mode": mode,
        "mode_label": LABELS.get(mode, mode),
        "owner": "agent" if status.get("reserved_for_agent") else "engineer",
        "busy": bool(status.get("engineer_busy")),
        "waiting": int(status.get("waiting_for_engineer") or 0),
        "activity": str(status.get("activity") or "idle"),
        "running": status.get("running_method") or None,
    }


def target(params: dict) -> str:
    """The mode an ``nx_set_control`` call asks for (``status`` for a read)."""
    mode = params.get("mode", "status")
    return ALIASES.get(mode, mode)


def envelope_from_snapshot(snapshot: dict, params: dict) -> dict:
    """A status read answered from the host's snapshot, shaped like the kernel's."""
    return {
        "tool": "nx_set_control",
        "operation_id": params.get("operation_id"),
        "ok": True,
        "mutation_outcome": "not_applicable",
        "frame": None,
        "data": control_data(snapshot),
        "warnings": [],
    }


@tool(
    effect="session",
    label="Set NX control",
    summary={
        "status": "Check who controls NX",
        "auto": "Switch NX MCP to Auto",
        "agent": "Take control of NX",
        "ask": "Switch NX MCP to Ask me first",
        "manual": "Hand control of NX back to the engineer",
    },
    variants="mode",
    keywords=(
        "panel mode",
        "agent drives",
        "ask me first",
        "auto mode",
        "hand back control",
        "who controls nx",
        "ui control",
    ),  # fmt: skip
    requires=("interactive",),
    cost="light",
    example={"mode": "status"},
)
def nx_set_control(
    tx: Tx, *, mode: Literal["status", "auto", "agent", "ask", "manual"] = "status"
) -> ControlData:
    """Read or change the NX MCP panel mode. auto: steps run between the engineer's
    commands. agent: NX stays locked for you. ask (= manual): each step waits for the
    engineer's OK. Lowering your control is immediate; more control waits up to 60 s
    for the engineer on the panel (else ask in chat)."""
    services = tx.services
    wanted = ALIASES.get(mode, mode)
    if wanted == "status":
        status = services.control_status()
        if status is None:
            fail("NX_VIEWPORT_UNAVAILABLE", "No NX MCP panel in this session",
                 hint="Run the bridge in interactive NX with the NX MCP panel")  # fmt: skip
        tx.outcome = "not_applicable"
        return control_data(status)
    with tx.checks():  # a refused raise changed nothing
        status = services.control(wanted)
    return control_data(status)

"""``nx_activate_application``: switch NX between Modeling and Sheet Metal.

It replaces ``nx_sheet_metal_context`` (tool-surface 3.1); sheet-metal features are its
reason to exist (``application:sheet_metal`` precondition).

NX 2406 findings encoded here:

- ``Session.ApplicationSwitchImmediate("UG_APP_SBSM")`` enters Sheet Metal for the
  displayed part only, so the work part must be the displayed part; the switch is
  verified with ``Session.ApplicationName``.
- Sheet-metal features fail in Modeling instead of switching silently: the kernel's
  ``application:sheet_metal`` precondition names this tool.
- Switching a part that holds sheet-metal features from Sheet Metal to Modeling opens a
  modal warning ("Sheet metal features added in the modeling environment may not be
  properly depicted in a flat pattern...") that blocks NX and every call. That switch is
  refused before NX is touched; Sheet Metal runs the modeling tools as well.
- NX remembers per part the application last entered while it was displayed and
  re-enters it on a later close or display (``kit.parts`` notes): a part shown in
  Sheet Metal without a switch (created there, say) would come back in Modeling with
  that warning. Sheet Metal is therefore entered even when NX already runs it.
- Entering an application the seat cannot license opens NX's blocking "License Not
  Available" dialog: the application's license feature is checked first
  (``kit.licenses``) and a missing one refused with ``NX_LICENSE_UNAVAILABLE``.
"""

from __future__ import annotations

import contextlib
from typing import Any, Literal, NoReturn, TypedDict

from nx_mcp.core import target as targets
from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.kit import drawing, licenses, parts, switching

APPLICATIONS = {"modeling": "UG_APP_MODELING", "sheet_metal": "UG_APP_SBSM"}
# License features of each application (NX 2406 feature names).
LICENSES = {"modeling": ("solid_modeling",), "sheet_metal": ("nx_sheet_metal",)}
NAMES = {
    "UG_APP_MODELING": "modeling",
    "UG_APP_SBSM": "sheet_metal",
    "UG_APP_DRAFTING": "drafting",
}


def _not_displayed(tx: Tx, part, display) -> NoReturn:
    """Refuse a target that is not the displayed part, before NX is touched: NX runs one
    application per session, entered for the displayed part, and the kernel restores the
    display and application a call switched, so entering it for a background part would
    be undone when the call ends."""
    named = getattr(getattr(tx, "target", None), "source", None) == "part"
    if display is None:
        fail("NX_WRONG_PART_CONTEXT", "No part is displayed; NX enters an application for the "
             "displayed part", hint="Display the part with nx_activate_part(display=true)")  # fmt: skip
    fail(
        "NX_WRONG_PART_CONTEXT",
        f"NX enters an application for the displayed part ({switching.file_name(display)}), "
        f"not for {switching.file_name(part)}",
        field="/part" if named else None,
        hint=f"Display {switching.file_name(part)} first with nx_activate_part(display=true), "
        "or pass the displayed part; sheet-metal tools called with part enter Sheet Metal "
        "for that call themselves",
        details={"part": switching.file_name(part), "display": switching.file_name(display)},
    )


def _displayed(services, args) -> Any:
    """Gate 3t: the displayed part when it is the agent's (NX enters an application for
    the displayed part only), so no ``part`` is needed; else None (the usual rule: the
    engineer's displayed part is never switched without ``part``). A given ``part``
    wins; the handler refuses it when it is not displayed."""
    display = None
    with contextlib.suppress(Exception):
        display = services.session.Parts.Display
    if display is None or not targets.counts(services, display):
        return None
    return display


_displayed.yields_to_part = True  # type: ignore[attr-defined]


class ApplicationData(TypedDict):
    application: str
    previous: str
    changed: bool


def name_of(native_name) -> str:
    text = str(native_name or "")
    return NAMES.get(text, text.lower())


@tool(
    target=_displayed,
    switch="none",  # never displayed for the call: the switch would be undone at its end
    effect="session",
    label="Activate application",
    summary="Switch NX to {application}",
    keywords=(
        "sheet metal application",
        "modeling application",
        "switch application",
        "enter sheet metal",
        "UG_APP_SBSM",
        "leave nx in sheet metal",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch", "interactive"),
    cost="light",
    example={"application": "sheet_metal"},
)
def nx_activate_application(
    tx: Tx, *, application: Literal["modeling", "sheet_metal"]
) -> ApplicationData:
    """Switch NX to the Modeling or Sheet Metal application for the displayed part.
    Sheet-metal tools called with part enter Sheet Metal for their call themselves: this
    tool is needed only to leave NX in an application. A part with sheet-metal features
    cannot go back to modeling. Drawings: nx_activate_drawing_sheet."""
    wanted = APPLICATIONS[application]
    with tx.checks():
        part = tx.part
        session = tx.session
        display = session.Parts.Display
        if display is None or not parts.same_part(display, part):
            _not_displayed(tx, part, display)
        previous = str(session.ApplicationName or "")
        if application == "modeling" and previous != wanted and parts.has_sheet_metal(part):
            fail("NX_UNSUPPORTED_OPTION",
                 "NX shows a blocking warning dialog when a part with sheet-metal features "
                 "enters Modeling, so the part stays in Sheet Metal",
                 hint="Keep working in Sheet Metal; it runs the modeling tools too",
                 field="/application")  # fmt: skip
        switching = previous != wanted or application == "sheet_metal"
        if switching:  # before NX is asked: an unlicensed switch opens a blocking dialog
            features = LICENSES[application]
            licenses.require(tx, features,
                             f"This NX seat has no {features[0]} license for {application}",
                             hint="Tell the engineer the license is missing",
                             by_argument=True)  # fmt: skip
    if drawing.current(part) is not None or drawing.work_view_is_drawing(part):
        drawing.close_sheets(tx, part)
    if switching:
        with native(default="NX_WRONG_APPLICATION"):
            session.ApplicationSwitchImmediate(wanted)
    now = str(session.ApplicationName or "")
    if now != wanted:
        fail("NX_WRONG_APPLICATION", f"NX stayed in {name_of(now)}, not {application}",
             details={"application": name_of(now)})  # fmt: skip
    return {"application": application, "previous": name_of(previous), "changed": now != previous}

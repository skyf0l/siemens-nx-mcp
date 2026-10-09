"""Sheet-metal helpers shared by this domain's tools (this domain only).

NX 2406 findings encoded here:

- Sheet-metal authoring needs the Sheet Metal application (``UG_APP_SBSM``) on the
  displayed work part. The kernel checks it (``requires application:sheet_metal``) and
  ``nx_activate_application`` switches; features are never authored in Modeling.
- ``SheetmetalManager.GetInnerBendFaces`` returns the inner bend faces with their
  state; ``GetBendParameters(face)`` reports ``InnerRadius`` (part units), ``BendAngle``
  (degrees) and ``NeutralFactor``; ``GetBodyThickness`` the actual sheet thickness.
- NX 2406 enums stringify as integers, so names come from ``result.enum_name``.
"""

from __future__ import annotations

from typing import TypedDict

from nx_mcp.core.errors import NXError, fail
from nx_mcp.kit import result, select
from nx_mcp.kit.sketch import snake

APPLICATION = "UG_APP_SBSM"


def namespace(tx):
    """``NXOpen.Features.SheetMetal`` (imported by the host at start)."""
    return tx.nx.Features.SheetMetal


def manager(tx):
    """The work part's ``SheetmetalManager``."""
    found = getattr(tx.part.Features, "SheetmetalManager", None)
    if found is None:
        fail("NX_UNSUPPORTED_OPTION", "This NX installation has no sheet-metal manager")
    return found


def is_sheet_metal(tx, body) -> bool:
    try:
        return bool(manager(tx).IsSheetmetalBody(body))
    except NXError:
        raise
    except Exception:  # NX refuses the query for some non-sheet-metal bodies
        return False


def sheet_body(tx, ref, field="/body"):
    """A sheet-metal body of the work part (anything else is ``NX_WRONG_GEOMETRY``)."""
    body = select.one(tx, ref, "body", field=field)
    if not is_sheet_metal(tx, body):
        fail("NX_WRONG_GEOMETRY", "Select a sheet-metal body", field=field)
    return body


def owner_body(tx, obj, field):
    """The sheet-metal body owning a face or edge."""
    body = obj.GetBody()
    if body is None or not is_sheet_metal(tx, body):
        fail("NX_WRONG_GEOMETRY", "Select geometry of a sheet-metal body", field=field)
    return body


def thickness(tx, body) -> float:
    return result.measured(manager(tx).GetBodyThickness(body), "thickness")


class BendRow(TypedDict):
    face: result.Row
    state: str
    radius: float
    angle: float
    neutral_factor: float


def bend_data(tx, face) -> dict:
    """``{radius, angle, neutral_factor}`` of one inner bend face."""
    params = manager(tx).GetBendParameters(face)
    return {
        "radius": result.measured(params.InnerRadius, "bend radius"),
        "angle": result.measured(params.BendAngle, "bend angle"),
        "neutral_factor": result.measured(params.NeutralFactor, "neutral factor"),
    }


def bends(tx, body) -> list[tuple]:
    """``[(face, {state, radius, angle, neutral_factor})]`` of a sheet-metal body."""
    faces, states = manager(tx).GetInnerBendFaces(body)
    enum = namespace(tx).SheetmetalBendState
    out = []
    for face, state in zip(faces, states, strict=True):
        out.append((face, {"state": snake(result.enum_name(state, enum)), **bend_data(tx, face)}))
    return out


def bend_rows(tx, found) -> list[BendRow]:
    return [{"face": tx.ref(face, "face"), **data} for face, data in found]

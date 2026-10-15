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

from typing import Any, TypedDict

from nx_mcp.core.errors import NXError, fail
from nx_mcp.kit import result, select
from nx_mcp.kit.sketch import snake


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


def _of_flat_pattern(feature) -> bool:
    """A Flat Pattern feature or one of its own sub-features: NX 2406 builds the flat
    bodies with EXTRACT_BODY and SB_FLAT_SOLID features named ``FLAT_PATTERN(n:...)``."""
    if str(getattr(feature, "FeatureType", "")) == "FLAT_PATTERN":
        return True
    return str(getattr(feature, "JournalIdentifier", "")).startswith("FLAT_PATTERN(")


def is_flat_pattern(body) -> bool:
    """A body only Flat Pattern features made (NX lists it as sheet metal too)."""
    try:
        features = list(body.GetFeatures())
    except Exception:  # no feature query for this body: treat it as a part body
        return False
    return bool(features) and all(_of_flat_pattern(f) for f in features)


def flat_pattern_of(tx, body):
    """The FLAT_PATTERN feature whose sub-features (``FLAT_PATTERN(n:...)``) built a Flat
    Pattern body, or None."""
    names = set()
    try:
        features = list(body.GetFeatures())
    except Exception:  # no feature query for this body
        return None
    for feature in features:
        name = str(getattr(feature, "JournalIdentifier", ""))
        if name.startswith("FLAT_PATTERN(") and ":" in name:
            names.add(name.split(":", 1)[0] + ")")
    for feature in tx.part.Features:
        if str(getattr(feature, "FeatureType", "")) == "FLAT_PATTERN" and (
            str(getattr(feature, "JournalIdentifier", "")) in names or feature in features
        ):
            return feature
    return None


def formed_body(feature):
    """The formed (bent) body a Flat Pattern's centre lines point at, or None."""
    for method in ("GetBendUpCenterLines", "GetBendDownCenterLines"):
        lines = getattr(feature, method, None)
        for data in lines() if lines is not None else ():
            face = getattr(data, "FormedBodyObject", None)
            if face is not None:
                return face.GetBody()
    return None


def not_flat_pattern(tx, body, field):
    """Refuse a Flat Pattern body: NX keeps those flat copies on a hidden layer where they
    overlap the formed body's web; a feature built on one corrupts the flat pattern."""
    if not is_flat_pattern(body):
        return body
    feature = flat_pattern_of(tx, body)
    formed = formed_body(feature) if feature is not None else None
    details: dict[str, Any] = {"body": tx.ref(body, "body")}
    hint = "Use geometry of the formed body: nx_find_geometry(owner=<formed body>)"
    if feature is not None:
        details["flat_pattern"] = tx.ref(feature, "feature")
    if formed is not None:
        details["formed_body"] = row = tx.ref(formed, "body")
        hint = f"Use geometry of the formed body {row['id']}: nx_find_geometry(owner={row['id']})"
    fail("NX_WRONG_GEOMETRY", "This is a Flat Pattern body (NX's hidden flat copy), not the "
         "formed sheet-metal body", field=field, hint=hint, details=details)  # fmt: skip


def owner_body(tx, obj, field):
    """The formed sheet-metal body owning a face or edge (never a Flat Pattern body)."""
    body = obj.GetBody()
    if body is None or not is_sheet_metal(tx, body):
        fail("NX_WRONG_GEOMETRY", "Select geometry of a sheet-metal body", field=field)
    return not_flat_pattern(tx, body, field)


def is_plain_solid(tx, body) -> bool:
    """A solid body that is not sheet metal (yet)."""
    return bool(getattr(body, "IsSolidBody", False)) and not is_sheet_metal(tx, body)


def solid_owner(tx, obj, field):
    """The solid, not sheet-metal body owning a face or edge (convert, from_solid)."""
    body = obj.GetBody()
    if body is None or not is_plain_solid(tx, body):
        fail("NX_WRONG_GEOMETRY", "Select geometry of a solid body that is not sheet metal",
             field=field)  # fmt: skip
    return body


def thickness(tx, body) -> float:
    return result.measured(manager(tx).GetBodyThickness(body), "thickness")


class BendData(TypedDict):
    radius: float
    angle: float
    neutral_factor: float


class BendState(BendData):
    state: str


class BendRow(TypedDict):
    face: result.Row
    state: str
    radius: float
    angle: float
    neutral_factor: float


def bend_data(tx, face) -> BendData:
    """``{radius, angle, neutral_factor}`` of one inner bend face."""
    params = manager(tx).GetBendParameters(face)
    return {
        "radius": result.measured(params.InnerRadius, "bend radius"),
        "angle": result.measured(params.BendAngle, "bend angle"),
        "neutral_factor": result.measured(params.NeutralFactor, "neutral factor"),
    }


def bends(tx, body) -> list[tuple[Any, BendState]]:
    """``[(face, {state, radius, angle, neutral_factor})]`` of a sheet-metal body."""
    faces, states = manager(tx).GetInnerBendFaces(body)
    enum = namespace(tx).SheetmetalBendState
    out: list[tuple[Any, BendState]] = []
    for face, state in zip(faces, states, strict=True):
        out.append((face, {"state": snake(result.enum_name(state, enum)), **bend_data(tx, face)}))
    return out


def bend_rows(tx, found: list[tuple[Any, BendState]]) -> list[BendRow]:
    return [{"face": tx.ref(face, "face"), **data} for face, data in found]

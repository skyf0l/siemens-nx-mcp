"""``nx_get_bend_list`` and ``nx_export_bend_list``: the bends of a sheet-metal body as data
or as a CSV file (angle, radius, bend line length, K factor, allowance, deduction, up/down).

NX 2406 findings encoded here:

- ``GetInnerBendFaces``/``GetBendParameters`` give each bend's inner face, state, inner
  radius, angle (degrees) and neutral factor; rows keep that order (``index``).
- The bend line length is the longest straight edge of the inner bend face that does not
  border a thickness face (bent: the tangent lines; flat: the long sides of the strip).
  Where the tab's sides are slanted it is the sheet's width at the tangent line (a 100 mm
  edge between sides at 1:4 with radius 3 and thickness 2 gives 97.5, checked live).
- A Flat Pattern's ``GetBendUpCenterLines``/``GetBendDownCenterLines`` name the formed
  body's bend face of each centre line: the inner face of an up bend, the opposite (outer)
  face of a down bend; that is the direction relative to the Flat Pattern's upward face.
  Without a Flat Pattern the inner face's layer gives it (top layer: up).
- ``BendListBuilder.PopulateBendListFromView`` (the Flat Pattern's modeling view) lists the
  bend sequence IDs and names sorted by ID, without the bend each belongs to; it is
  returned as ``sequence``. ``EditBendItem`` + commit renumbers them (not offered: no
  bend can be named by reference).
- Allowance = rad(angle) * (radius + K * thickness): the flat bend strip's width.
  Deduction = 2 * (radius + thickness) * tan(angle / 2) - allowance; a hem (NX reports
  180.0000000001 degrees) has no outside setback, so its deduction is null.
- ``CleanUpUtilityBuilder`` (Clean-Up Utility) adds an unparameterized BREP copy of the
  solid beside the original (kept visible), with each sharp corner replaced by a bend of
  inner radius 0.01 * thickness; the copy is not a sheet-metal body. Convert to Sheet
  Metal already turns sharp corners into bends of the default radius, so it is not offered.
"""

from __future__ import annotations

import csv
import io
import math
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Offset, PageLimit, PathTo, RefTo
from nx_mcp.domains.sheet_metal import common
from nx_mcp.domains.sheet_metal.export import flat_pattern_feature
from nx_mcp.kit import builders, files, geom, licenses, result, switching

LICENSE = ("nx_sheet_metal",)
FlatPatternArg = Annotated[
    str, RefTo("feature", doc="Ref f:N: Flat Pattern; default: the body's first")
]
BodyArg = Annotated[
    str, RefTo("body", doc="Ref b:N; default: the part's only formed sheet-metal body")
]


class BendItem(TypedDict):
    index: int
    face: result.Row
    state: str
    direction: str | None
    angle: float
    radius: float
    length: float | None
    neutral_factor: float
    allowance: float
    deduction: float | None


class SequenceItem(TypedDict):
    id: int
    name: str


class BendListData(TypedDict):
    body: result.Row
    thickness: float
    flat_pattern: result.Row | None
    direction_from: Literal["flat_pattern", "top_layer"]
    items: list[BendItem]
    sequence: list[SequenceItem]
    page: dict[str, Any]


NO_SHEET_METAL_HINT = (
    "Convert a constant-thickness solid with nx_create_sheet_metal_feature(operation=convert) "
    "or start one with operation=tab"
)


def _body(tx, ref):
    if ref is not None:
        return common.sheet_body(tx, ref)
    found = [
        b for b in tx.part.Bodies if common.is_sheet_metal(tx, b) and not common.is_flat_pattern(b)
    ]
    if not found:
        fail("NX_NOT_FOUND", f"{switching.file_name(tx.part)} has no sheet-metal body",
             field="/body", hint=NO_SHEET_METAL_HINT)  # fmt: skip
    if len(found) != 1:
        fail("NX_TARGET_REQUIRED", f"Give body: the work part has {len(found)} sheet-metal "
             "bodies", field="/body",
             details={"candidates": [tx.ref(b, "body") for b in found]})  # fmt: skip
    return found[0]


def _same(a, b) -> bool:
    return a is not None and b is not None and getattr(a, "Tag", a) == getattr(b, "Tag", b)


def _directions(tx, feature) -> dict[Any, str]:
    """``{formed-body face tag: "up"/"down"}`` from a Flat Pattern's bend centre lines."""
    out: dict[Any, str] = {}
    for method, side in (("GetBendUpCenterLines", "up"), ("GetBendDownCenterLines", "down")):
        for data in getattr(feature, method)():
            face = getattr(data, "FormedBodyObject", None)
            if face is not None:
                out[face.Tag] = side
    return out


def _flat_pattern(tx, ref, body):
    """The Flat Pattern feature to use (``ref``, else the first one of ``body``) or None."""
    if ref is not None:
        feature = flat_pattern_feature(tx, ref)
        licenses.require(tx, LICENSE, "Reading a Flat Pattern needs a sheet-metal license")
        formed = common.formed_body(feature)
        if formed is not None and not _same(formed, body):
            fail("NX_WRONG_OWNER", "This Flat Pattern belongs to another body",
                 field="/flat_pattern")  # fmt: skip
        return feature
    if licenses.served(tx, LICENSE) is False:
        return None
    for feature in tx.part.Features:
        if str(getattr(feature, "FeatureType", "")) == "FLAT_PATTERN" and _same(
            common.formed_body(feature), body
        ):
            return feature
    return None


def _sequence(tx, feature) -> list[SequenceItem]:
    """NX's bend list (sequence IDs and names) of the Flat Pattern's modeling view."""
    manager = common.manager(tx)
    with builders.build(tx, manager.CreateFlatPatternBuilder, feature) as b:
        name = str(b.FlatPatternViewName or "")
    try:
        view = tx.part.ModelingViews.FindObject(name) if name else None
    except Exception:  # NX raises for a view name it does not know
        view = None
    if view is None:
        return []
    out: list[SequenceItem] = []
    with builders.build(tx, manager.CreateBendListBuilder) as b:
        b.PopulateBendListFromView(view)
        items = b.BendList
        for i in range(int(items.Length)):
            item = items.FindItem(i)
            out.append({"id": int(item.BendID), "name": str(item.BendName or "")})
    return sorted(out, key=lambda s: s["id"])


def _length(tx, face) -> float | None:
    """The bend line length: the longest straight edge not bordering a thickness face."""
    manager = common.manager(tx)
    thickness_type = common.namespace(tx).SheetmetalFaceType.Thickness
    linear = tx.nx.Edge.EdgeType.Linear
    best = None
    for edge in face.GetEdges():
        if edge.SolidEdgeType != linear:
            continue
        others = [f for f in edge.GetFaces() if not _same(f, face)]
        if any(manager.GetFaceType(f) == thickness_type for f in others):
            continue
        a, b = edge.GetVertices()
        size = geom.dist(geom.xyz(a), geom.xyz(b))
        best = size if best is None else max(best, size)
    return best


def _direction(tx, face, sides) -> str | None:
    manager = common.manager(tx)
    if sides is not None:
        if face.Tag in sides:
            return sides[face.Tag]
        try:
            opposite = manager.GetOppositeFace(face)
        except Exception:  # no opposite face: NX cannot tell
            opposite = None
        if opposite is not None and opposite.Tag in sides:
            return sides[opposite.Tag]
        return None
    layers = common.namespace(tx).SheetmetalFaceLayer
    layer = manager.GetFaceLayer(face)
    return "up" if layer == layers.Top else "down" if layer == layers.Bottom else None


def allowance(angle: float, radius: float, k: float, thickness: float) -> float:
    return math.radians(angle) * (radius + k * thickness)


HEM_ANGLE = 179.9  # degrees: from here on tan(angle / 2) has no meaningful setback


def deduction(angle: float, radius: float, k: float, thickness: float) -> float | None:
    """The bend deduction, or None for a hem (about 180 degrees: no outside setback)."""
    if angle >= HEM_ANGLE:
        return None
    setback = (radius + thickness) * math.tan(math.radians(angle) / 2.0)
    return 2.0 * setback - allowance(angle, radius, k, thickness)


def bend_list(tx, body_ref, flat_ref) -> tuple[Any, Any, list[BendItem], list[SequenceItem]]:
    body = _body(tx, body_ref)
    feature = _flat_pattern(tx, flat_ref, body)
    sides = _directions(tx, feature) if feature is not None else None
    t = common.thickness(tx, body)
    items: list[BendItem] = []
    for n, (face, data) in enumerate(common.bends(tx, body), 1):
        tx.checkpoint()
        a, r, k = data["angle"], data["radius"], data["neutral_factor"]
        items.append(
            {
                "index": n,
                "face": tx.ref(face, "face"),
                "state": data["state"],
                "direction": _direction(tx, face, sides),
                "angle": a,
                "radius": r,
                "length": _length(tx, face),
                "neutral_factor": k,
                "allowance": allowance(a, r, k, t),
                "deduction": deduction(a, r, k, t),
            }
        )
    sequence = _sequence(tx, feature) if feature is not None else []
    return body, feature, items, sequence


@tool(
    switch="work",  # like nx_get_sheet_metal: NX answers in the work part's units
    effect="read",
    label="Get bend list",
    summary=lambda a, say: f"Bend list of {say('body')}" if a.get("body") else "Bend list",
    keywords=(
        "bend list",
        "bend table",
        "bend allowance",
        "bend deduction",
        "bend direction",
        "bend sequence",
        "k factor",
        "press brake",
    ),  # fmt: skip
    requires=("work_part",),
    cost="light",
    example={"body": "b:4"},
)
def nx_get_bend_list(
    tx: Tx,
    *,
    body: BodyArg | None = None,
    flat_pattern: FlatPatternArg | None = None,
    offset: Offset = 0,
    limit: PageLimit = 50,
) -> BendListData:
    """List the bends of a sheet-metal body with angle, radius, length, K, allowance and up/down.
    Per bend: angle (deg), inner radius, bend line length, K factor, allowance (flat strip
    width), deduction (null for 180-degree hems). Up/down is relative to a Flat Pattern's upward face (direction_from),
    else the top layer; sequence: NX's bend list IDs and names of that Flat Pattern. CSV:
    nx_export_bend_list."""
    with tx.checks():
        found, feature, items, sequence = bend_list(tx, body, flat_pattern)
    selected, page = result.page(items, offset, limit)
    return {
        "body": tx.ref(found, "body"),
        "thickness": common.thickness(tx, found),
        "flat_pattern": tx.ref(feature, "feature") if feature is not None else None,
        "direction_from": "flat_pattern" if feature is not None else "top_layer",
        "items": selected,
        "sequence": sequence,
        "page": page,
    }


COLUMNS = ("index", "direction", "angle", "radius", "length", "neutral_factor", "allowance",
           "deduction", "state")  # fmt: skip


def _cell(value):
    """A CSV cell: floats to 6 decimals (NX's 89.99999999999997 reads 90.0), None empty."""
    if value is None:
        return ""
    return round(value, 6) if isinstance(value, float) else value


def csv_text(items: list[BendItem], sequence: list[SequenceItem], thickness, units) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for item in items:  # no face ref: it means nothing outside this NX session
        row: dict[str, Any] = dict(item)
        writer.writerow([_cell(row[c]) for c in COLUMNS])
    writer.writerow([])
    writer.writerow(["thickness", _cell(thickness)])
    writer.writerow(["units", f"lengths {units}; angles deg"])
    if sequence:
        writer.writerow([])
        writer.writerow(["sequence_id", "sequence_name"])
        for s in sequence:
            writer.writerow([s["id"], s["name"]])
    return buffer.getvalue()


class ExportData(TypedDict):
    path: str
    size: int
    sha256: str
    bend_count: int
    thickness: float
    direction_from: str
    overwritten: bool


@tool(
    switch="work",
    effect="file",
    label="Export bend list (CSV)",
    summary=lambda a, say: f"Bend list to {say('path')}",
    keywords=("bend list csv", "bend table export", "press brake list", "bend report"),
    requires=("work_part",),
    cost="light",
    example={"path": "reports/bends.csv", "body": "b:4"},
)
def nx_export_bend_list(
    tx: Tx,
    *,
    path: Annotated[str, PathTo("replace", ".csv")],
    body: BodyArg | None = None,
    flat_pattern: FlatPatternArg | None = None,
    overwrite: bool = False,
) -> ExportData:
    """Write a sheet-metal body's bend list to an Excel-friendly .csv file. Lines: the
    nx_get_bend_list rows (all of them, without face refs; a hem's deduction is empty),
    thickness, units, then the sequence IDs. The model is not changed; overwrite replaces a
    file."""
    target = Path(path)
    replacing = target.exists()
    if replacing and not overwrite:
        fail("NX_ALREADY_EXISTS", "/path: a file already exists there; set overwrite=true to "
             "replace it", field="/path",
             details={"path": files.relative(path, tx.workspace.root)})  # fmt: skip
    with tx.checks():
        found, feature, items, sequence = bend_list(tx, body, flat_pattern)
        t = common.thickness(tx, found)
    temp = tx.files.stage(target, replace=overwrite)
    units = tx.units.get("length", "mm")
    temp.write_text(csv_text(items, sequence, t, units), encoding="utf-8-sig", newline="")
    published = tx.files.publish()[0]
    return {
        **files.metadata(published, tx.workspace.root),
        "bend_count": len(items),
        "thickness": t,
        "direction_from": "flat_pattern" if feature is not None else "top_layer",
        "overwritten": replacing,
    }

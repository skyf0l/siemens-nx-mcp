"""Crosshatch and area fill of a closed region of sheet curves.

NX 2406 findings encoded here:

- ``HatchBuilder.Boundary`` takes sections: a section over the boundary curves is
  appended to ``CurveBoundaries`` and the boundary's view is the sheet's own view
  (``Boundary.SetView(sheet.View)``); without the view the commit fails ("The input
  object is a null entity").
- The hatch is view dependent in the sheet's view; its box (UF
  ``ModlGeneral.AskBoundingBox``) is the boundary's box. A crosshatch's origin is the
  region's centre, an area fill's is [0, 0]: the box centre is reported instead.
- Crosshatch patterns are the names of NX's ``xhatch.chx`` (``Iron/General Use`` default,
  45 degrees, spacing 5 in part units); area fill takes an ``AreaFillMaterial``.
- A patterned area fill (concrete, sand, ...) draws whole pattern elements: its box stops
  short of the boundary (a 30 mm circle of concrete: 29.3 x 29.3), so it is checked to lie
  inside the region instead of matching it.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, Doc, Items, Range, RefTo, SheetLength
from nx_mcp.domains.drawing.common import commit, quiet_sheet_sketch, update
from nx_mcp.kit import args, builders, drawing, licenses, readback, result, select
from nx_mcp.kit import sketch as ksketch

PATTERNS = {
    "iron": "Iron/General Use",
    "steel": "Steel",
    "brass": "Brass/Copper",
    "rubber": "Rubber/Plastic",
    "refractory": "Refractory",
    "marble": "Marble/Slate/Glass",
    "lead": "Lead",
    "aluminum": "Aluminum/Magnesium",
    "electrical_winding": "Electrical Winding",
    "thermal_insulation": "Thermal Insulation",
}
MATERIALS = {
    "solid": "SolidFill",
    "concrete": "Concrete",
    "cork_felt": "CorkFelt",
    "earth": "Earth",
    "liquids": "Liquids",
    "rock": "Rock",
    "sand": "Sand",
    "sound_insulation": "SoundInsulation",
    "wood_across_grain": "WoodAcrossGrain",
    "wood_along_grain": "WoodAlongGrain",
}
Pattern = Literal[
    "iron", "steel", "brass", "rubber", "refractory", "marble", "lead", "aluminum",
    "electrical_winding", "thermal_insulation",
]  # fmt: skip
Material = Literal[
    "solid", "concrete", "cork_felt", "earth", "liquids", "rock", "sand", "sound_insulation",
    "wood_across_grain", "wood_along_grain",
]  # fmt: skip
IDENTITY = {"origin": [0.0, 0.0, 0.0], "x_axis": [1.0, 0.0, 0.0], "y_axis": [0.0, 1.0, 0.0],
            "normal": [0.0, 0.0, 1.0]}  # fmt: skip
DEFAULT_ANGLE = {"crosshatch": 45.0, "area_fill": 0.0}
# CreateHatchBuilder takes either license feature (NX's NXOpen reference).
LICENSES = ("drafting", "nx_2d_edit")
PATTERN_COVER = 0.5  # a patterned fill's box spans at least half the region on each axis


class HatchData(TypedDict, total=False):
    annotation: result.Row
    sheet: result.Row
    type: str
    pattern: str
    material: str
    angle: float
    spacing: float
    scale: float
    center: Annotated[list[float], Doc("Centre of the hatch's box on the sheet")]
    bounds: list[float]
    boundary: dict


def _boundary(tx, refs):
    """``(curves, sheet, box)``: the boundary curves, the one sheet they are drawn on and
    their box ``[x0, y0, x1, y1]`` in part units; refuses an open or branched chain."""
    curves = [
        select.resolve(tx, r, "curve", field=f"/boundary/{i}", work_part=True)
        for i, r in enumerate(refs)
    ]
    sheets = {}
    for i, c in enumerate(curves):
        sheet = drawing.sheet_of_annotation(tx, c)
        if sheet is None:
            fail("NX_WRONG_OWNER", "Boundary curves must be drawn on a drawing sheet",
                 field=f"/boundary/{i}",
                 hint="Draw them with nx_create_sheet_sketch")  # fmt: skip
        sheets[int(sheet.Tag)] = sheet
    if len(sheets) != 1:
        fail("NX_INVALID_ARGUMENT", "The boundary curves lie on several sheets",
             field="/boundary")  # fmt: skip
    rows = []
    for i, c in enumerate(curves):
        if ksketch.curve_kind(tx, c) == "other":
            fail("NX_WRONG_GEOMETRY", "Boundary curves must be lines, arcs or circles",
                 field=f"/boundary/{i}")  # fmt: skip
        rows.append(ksketch.read(tx, IDENTITY, c))
    loops = ksketch.loops(rows)
    if len(loops) != 1 or not loops[0].get("closed") or loops[0].get("branched"):
        fail(
            "NX_INVALID_ARGUMENT",
            "The boundary curves must form one closed loop",
            field="/boundary",
            details={"loops": loops},
            hint="Pass every curve of one closed loop (a rectangle, closed polyline or circle)",
        )
    xs, ys = [], []
    for c, row in zip(curves, rows, strict=True):
        if row["type"] == "circle":
            (cx, cy), r = row["center"], row["radius"]
            xs += [cx - r, cx + r]
            ys += [cy - r, cy + r]
        else:
            xs += [row["start"][0], row["end"][0]]
            ys += [row["start"][1], row["end"][1]]
            if row["type"] == "arc":  # the arc's own box (its bulge may pass the ends)
                box = [float(v) for v in tx.uf.ModlGeneral.AskBoundingBox(c.Tag)]
                xs += [box[0], box[3]]
                ys += [box[1], box[4]]
    return curves, next(iter(sheets.values())), [min(xs), min(ys), max(xs), max(ys)]


def _read(tx, hatch):
    A = tx.nx.Annotations
    with builders.build(tx, tx.part.Annotations.Hatches.CreateHatchBuilder, hatch) as r:
        kind = result.enum_name(r.AnnotationType, A.HatchBuilder.AnnotationTypes,
                                ["Crosshatch", "AreaFill"])  # fmt: skip
        return {
            "type": "area_fill" if kind == "AreaFill" else "crosshatch",
            "pattern": str(r.Pattern),
            "angle": float(r.Angle),
            "distance": float(r.Distance),
            "material": result.enum_name(
                r.Material, A.AreaFillMaterial, list(MATERIALS.values())
            ),  # fmt: skip
            "fill_angle": float(r.AreafillAngle),
            "scale": float(r.Scale),
            "boundaries": int(r.Boundary.CurveBoundaries.Length),
        }


def fills(bounds, region, tol, patterned=False):
    """True when a hatch's box fills the boundary's region: the same box for a crosshatch
    or a solid fill; a patterned area fill (concrete, sand, ...) draws whole pattern
    cells, so its box only lies inside the region and covers part of each axis."""
    if not patterned:
        return all(abs(a - b) <= tol for a, b in zip(bounds, region, strict=True))
    inside = all(bounds[i] >= region[i] - tol and bounds[i + 2] <= region[i + 2] + tol
                 for i in (0, 1))  # fmt: skip
    covered = all(bounds[i + 2] - bounds[i] >= PATTERN_COVER * (region[i + 2] - region[i])
                  for i in (0, 1))  # fmt: skip
    return inside and covered


def _close_angle(got, want):
    error = (float(got) - float(want)) % 360.0
    return min(error, 360.0 - error) <= 1e-6


@tool(
    effect="model",
    label="Create hatch",
    summary=lambda a, say: (
        f"{'Area fill' if a.get('type') == 'area_fill' else 'Crosshatch'} of "
        f"{len(a.get('boundary') or [])} curves"
    ),
    keywords=(
        "crosshatch",
        "hatch",
        "area fill",
        "section lining",
        "laminate",
        "fill region",
        "solid fill",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={"boundary": ["cu:5", "cu:6", "cu:7", "cu:8"], "pattern": "steel", "spacing": 3},
)
def nx_create_hatch(
    tx: Tx,
    *,
    boundary: Annotated[
        list[Annotated[str, RefTo("curve")]],
        Items(1, 200),
        Doc("Curves on one sheet forming one closed loop (nx_create_sheet_sketch curves)"),
    ],
    type: Literal["crosshatch", "area_fill"] = "crosshatch",  # noqa: A002
    pattern: Annotated[Pattern, Doc("crosshatch: NX pattern")] = "iron",
    angle: Annotated[Angle, Doc("Line or fill angle from sheet +x; default 45 (fill 0)")]
    | None = None,
    spacing: Annotated[SheetLength, Doc("crosshatch: line distance; default NX's (5 mm)")]
    | None = None,
    material: Annotated[Material, Doc("area_fill: NX fill pattern")] = "solid",
    scale: Annotated[float, Range(gt=0, le=100), Doc("area_fill: pattern scale")] = 1.0,
) -> HatchData:
    """Fill a closed region of a drawing sheet with a crosshatch or an area fill. boundary:
    the curves of one closed loop drawn on the sheet (nx_create_sheet_sketch: a rectangle,
    closed polyline or circle); several hatches with other angles or spacings tell
    laminate layers apart. pattern, spacing apply to crosshatch; material, scale to
    area_fill. Section views hatch their cut faces on their own."""
    args.distinct("/boundary", boundary, "boundary")
    if type == "crosshatch" and (material != "solid" or scale != 1.0):
        fail("NX_INVALID_ARGUMENT", "material and scale apply to area_fill",
             field="/material" if material != "solid" else "/scale")  # fmt: skip
    if type == "area_fill" and (pattern != "iron" or spacing is not None):
        fail("NX_INVALID_ARGUMENT", "pattern and spacing apply to crosshatch",
             field="/pattern" if pattern != "iron" else "/spacing")  # fmt: skip
    if spacing is not None and not float(spacing) > 0:
        fail("NX_INVALID_ARGUMENT", "spacing must be positive", field="/spacing")
    with tx.checks():
        curves, sheet, box = _boundary(tx, boundary)
    wanted = DEFAULT_ANGLE[type] if angle is None else float(angle)
    A, part = tx.nx.Annotations, tx.part
    with licenses.attempt(tx, LICENSES), drawing.on_sheet(tx, sheet, part) as sh:
        quiet_sheet_sketch(tx)
        start = ksketch.read(tx, IDENTITY, curves[0])["start"]
        section = builders.curve_section(tx, curves, start=[*start, 0.0])
        with builders.build(tx, part.Annotations.Hatches.CreateHatchBuilder, A.Hatch.Null) as b:
            b.Boundary.SelectionType = A.BoundaryBuilder.SelectionMethod.Curves
            b.Boundary.SetView(sheet.View)
            b.Boundary.CurveBoundaries.Append(section)
            if type == "crosshatch":
                b.AnnotationType = A.HatchBuilder.AnnotationTypes.Crosshatch
                b.Pattern = PATTERNS[pattern]
                b.Angle = wanted
                if spacing is not None:
                    b.Distance = float(spacing) * sh.scale
            else:
                b.AnnotationType = A.HatchBuilder.AnnotationTypes.AreaFill
                b.Material = getattr(A.AreaFillMaterial, MATERIALS[material])
                b.AreafillAngle = wanted
                b.Scale = float(scale)
            hatch = commit(b, "hatch")
        update(tx)
        got = _read(tx, hatch)
        readback.expect(got["type"] == type and got["boundaries"] >= 1, "type", **got)
        out: HatchData = {
            "annotation": tx.ref(hatch, "annotation"),
            "sheet": tx.ref(sheet, "drawing_sheet"),
            "type": type,
        }
        if type == "crosshatch":
            readback.expect(got["pattern"] == PATTERNS[pattern] and _close_angle(
                got["angle"], wanted), "pattern", **got)  # fmt: skip
            if spacing is not None:
                readback.expect_close(got["distance"] / sh.scale, float(spacing), "spacing",
                                      1e-6 * float(spacing))  # fmt: skip
            out["pattern"], out["angle"] = pattern, got["angle"]
            out["spacing"] = got["distance"] / sh.scale
        else:
            readback.expect(got["material"] == MATERIALS[material] and _close_angle(
                got["fill_angle"], wanted), "material", **got)  # fmt: skip
            readback.expect_close(got["scale"], float(scale), "scale", 1e-9 * float(scale))
            out["material"], out["angle"], out["scale"] = material, got["fill_angle"], got["scale"]
        bounds = drawing.annotation_box(tx, hatch, sh)[0]
        region = [*sh.to_sheet(box[0:2]), *sh.to_sheet(box[2:4])]
        if bounds is not None:
            tol = 1e-3 * max(1.0, *map(abs, region))
            patterned = type == "area_fill" and material != "solid"
            readback.expect(
                fills(bounds, region, tol, patterned),
                "bounds",
                "The hatch does not fill the boundary's region",
                bounds=bounds,
                region=region,
            )
            out["bounds"] = bounds
            out["center"] = [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2]
        out["boundary"] = {"curves": len(curves), "region": region,
                           "size": [region[2] - region[0], region[3] - region[1]]}  # fmt: skip
        return out

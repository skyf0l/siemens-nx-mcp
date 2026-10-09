"""Sew, thicken and trim sheet bodies.

NX 2406 lessons kept here: a native refusal (sheets that do not touch within
tolerance, 4635012; a trim boundary that misses the sheet, 671228) is
``NX_BUILD_FAILED`` with ``nx_code``. Sew can leave bodies unsewn or fall back to
a sheet when a solid was asked for; both are ``NX_INCOMPLETE_RESULT`` and roll
back. Tolerances default to 0.001 mm in part units (inch parts scale).
"""

from __future__ import annotations

from typing import Annotated

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, Point3, RefTo
from nx_mcp.domains.freeform import common
from nx_mcp.kit import builders, curve, geom, measure, readback, select, units

# ---------------------------------------------------------------------------
# Sew


class SewData(common.SheetData):
    solid: bool
    volume: float | None


@tool(
    effect="model",
    label="Sew sheets",
    summary=lambda a, say: (
        f"Sew {len(a['tools']) + 1} sheets" + (" into a solid" if a.get("solid") else "")
    ),
    keywords=("sew", "knit", "stitch sheets", "join surfaces", "close into solid"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"target": "b:3", "tools": ["b:5"]},
)
def nx_create_sew(
    tx: Tx,
    *,
    target: RefTo("body"),  # type: ignore[valid-type]
    tools: Annotated[list[RefTo("body")], Items(1, 100)],  # type: ignore[valid-type]
    tolerance: Annotated[Length, Doc("Largest gap to close")] | None = None,
    solid: Annotated[bool, Doc("Require a closed solid result")] = False,
) -> SewData:
    """Sew sheet bodies into the target sheet; every tool must sew. solid=true requires a
    closed solid and fails when NX can only make a sheet."""
    if target in tools:
        fail("NX_INVALID_ARGUMENT", "target cannot also be a tool", field="/tools")
    with tx.checks():
        tol = common.given_tolerance(tx, tolerance)
        body = common.sheet(tx, target, "/target")
        others = common.sheets(tx, tools, "/tools")
        if int(body.Tag) in {int(t.Tag) for t in others}:
            fail("NX_INVALID_ARGUMENT", "target cannot also be a tool", field="/tools")
    F = tx.nx.Features
    part = tx.part
    with builders.build(tx, part.Features.CreateSewBuilder, None) as b:
        b.Type = F.SewBuilder.Types.Sheet
        b.BodyPreference = getattr(F.SewBuilder.BodyPreferenceTypes, "Solid" if solid else "Sheet")
        b.Tolerance = tol
        b.TargetBodiesCollector.ReplaceRules([part.ScRuleFactory.CreateRuleBodyDumb([body])], False)
        b.ToolBodiesCollector.ReplaceRules([part.ScRuleFactory.CreateRuleBodyDumb(others)], False)
        feature = b.commit()
        unsewn = list(b.GetUnsewnBodies() or [])
    if unsewn:
        fail(
            "NX_INCOMPLETE_RESULT",
            f"{len(unsewn)} bodies could not be sewn; the sew was rolled back",
            hint="Close the gaps or raise tolerance",
            details={"unsewn": len(unsewn)},
        )
    out = common.result_of(tx, feature)
    bodies = common.feature_bodies(feature)
    closed = all(not common.is_sheet(b) for b in bodies)
    if solid and not closed:
        fail(
            "NX_INCOMPLETE_RESULT",
            "NX produced a sheet instead of the requested solid",
            hint="The sheets do not enclose a volume; sew without solid or close the gaps",
        )
    volume = measure.volume(tx, bodies) if closed else None
    if volume is not None:
        readback.expect(volume > 0, "volume", "The sewn solid has no volume", volume=volume)
    return {**out, "solid": closed, "volume": volume}


# ---------------------------------------------------------------------------
# Thicken


class ThickenData(common.SheetData):
    volume: float


@tool(
    effect="model",
    label="Thicken faces",
    summary="Thicken {faces} by {offset1}",
    keywords=("thicken", "offset sheet to solid", "give thickness", "solid from surface"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:7"], "offset1": 2},
)
def nx_create_thicken(
    tx: Tx,
    *,
    faces: Annotated[list[RefTo("face")], Items(1, 200)],  # type: ignore[valid-type]
    offset1: Annotated[Length, Doc("First offset along the face normal")],
    offset2: Annotated[Length, Doc("Second offset along the face normal")] = 0.0,
) -> ThickenData:
    """Thicken sheet faces into a new solid between two signed offsets along the face
    normals."""
    first, second = float(offset1), float(offset2)
    if geom.close(first, second, 1e-9):
        fail("NX_INVALID_ARGUMENT", "offset1 and offset2 must differ", field="/offset2")
    with tx.checks():
        selected = select.faces(tx, faces, "/faces")
    part = tx.part
    with builders.build(tx, part.Features.CreateThickenBuilder, None) as b:
        b.FaceCollector.ReplaceRules([part.ScRuleFactory.CreateRuleFaceDumb(selected)], False)
        b.Tolerance = units.part_tolerance(tx)
        b.FirstOffset.RightHandSide = repr(first)
        b.SecondOffset.RightHandSide = repr(second)
        feature = b.commit(validate=True)
    out = common.result_of(tx, feature)
    bodies = [b for b in common.feature_bodies(feature) if not common.is_sheet(b)]
    readback.expect(bool(bodies), "solid", "NX built no solid")
    volume = measure.volume(tx, bodies)
    readback.expect(volume > 0, "volume", "The thickened solid has no volume", volume=volume)
    if len(selected) == 1 and select.face_data(tx, selected[0])[0] == select.FACE_PLANE:
        wanted = measure.area(tx, selected) * abs(first - second)
        readback.expect_close(volume, wanted, "volume", tol=1e-6 * max(1.0, wanted))
    return {**out, "volume": volume}


# ---------------------------------------------------------------------------
# Trim sheet


@tool(
    effect="model",
    label="Trim sheet",
    summary=lambda a, say: (
        f"Trim {say('body')} by {say('boundaries')}"
        + ("" if a.get("keep", True) else ", discard region")
    ),
    keywords=("trim sheet", "trim surface", "cut sheet with curves", "split surface"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"body": "b:3", "boundaries": ["c:5"], "region_point": [5, 5, 0]},
)
def nx_create_trim_sheet(
    tx: Tx,
    *,
    body: RefTo("body"),  # type: ignore[valid-type]
    boundaries: Annotated[list[RefTo("curve")], Items(1, 100)],  # type: ignore[valid-type]
    region_point: Annotated[Point3, Doc("A point on the region to keep or discard")],
    keep: Annotated[bool, Doc("Keep the region (false: discard it)")] = True,
) -> common.SheetData:
    """Trim a sheet body with boundary curves that cross it; keep or discard the region that
    holds region_point."""
    point = [float(v) for v in region_point]
    with tx.checks():
        target = common.sheet(tx, body)
        curves = select.curves(tx, boundaries, "/boundaries")
    before, _ = curve.point_distance(tx, target, point)
    low, high = measure.bounds(tx, target)
    part = tx.part
    F = tx.nx.Features
    with builders.build(tx, part.Features.CreateTrimsheetBuilder, None) as b:
        b.TargetBodies.Add(target)
        b.BoundaryObjects.Add(builders.curve_section(tx, curves))
        options = F.TrimSheetBuilder.KeepDiscardOption
        b.KeepDiscardMethod = options.Keep if keep else options.Discard
        b.Tolerance = units.part_tolerance(tx)
        b.OutputExactGeometry = True
        b.Regions.Append(part.CreateRegionPoint(builders.point(tx, point), target))
        feature = b.commit()
    bodies = common.feature_bodies(feature) or [target]
    out = common.result_of(tx, feature, bodies)
    after = min(curve.point_distance(tx, x, point)[0] for x in bodies)
    tol = max(units.part_tolerance(tx), geom.tol(low, high))
    if keep:
        readback.expect(after <= before + tol, "kept_region", before=before, after=after)
    else:
        readback.expect(after > before + tol, "discarded_region", before=before, after=after)
    return out

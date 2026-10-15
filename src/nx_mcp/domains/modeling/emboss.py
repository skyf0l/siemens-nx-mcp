"""Emboss (a pad or pocket from a section onto faces) and Offset Emboss (a raised strip on a
sheet along a curve). The tool-body emboss is ``nx_create_emboss_body`` (shapes).

NX 2406 lessons kept here (probed live on an 80 x 80 x 10 block and a 100 x 60 sheet):

- ``Features.CreateEmbossBuilder`` comes with one region (``GetRegions()[0]``), one limit
  and one target. The region's ``Section`` is a new section of the sketch curves; the
  target's ``TargetFace`` takes a face collector (a face raises "Expecting ScCollector");
  ``EmbossLimit.SetDistance`` sets the end cap distance. ``EmbossBuilder.Tolerance``
  defaults to 0, which fails every emboss ("Unable to create emboss geometry"): it is set
  to the part tolerance.
- The end cap lies at the signed distance from the target faces: a pad (convexity Above)
  takes +height, a pocket (Below) -height (+3 for a pocket is refused: "The relative
  position of section, end cap and target are incorrect"). A circle r 15 sketched 20 above
  the top face: pad 5 adds exactly pi 15^2 5 = 3534.29, pocket 3 removes 2120.58; the walls
  follow the section along the sketch normal. The region direction, the projection option
  and the sidewall angle did not change the result, so they stay at NX's defaults.
  ``EndcapOption`` OffsetExact offsets the target faces (follows their curvature),
  Translation moves them.
- ``Features.CreateOffsetEmbossBuilder``: ``Target`` takes the sheet's faces (a body rule
  is refused: "invalid or unsuitable OM object"), ``CenterLine`` is the builder's own
  section; ``HeightOffset``, ``LeftWidth``, ``RightWidth``, ``SideOffset`` are expressions.
  On the plane z 0 a 60 long line with height 3, widths 5 + 5 and side offset 2 raised the
  sheet to z 3 (area 6000 -> 6211.93); the feature keeps the sheet body.
  ``ReverseDirection`` presses the strip in (a groove). The tolerances default to 0 and are
  set (part tolerance, 0.5 deg).
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Doc, FaceRef, Items, Length, Point3, RefTo, SketchRef
from nx_mcp.domains.modeling import common
from nx_mcp.kit import args, builders, curve, expr, measure, readback, result, select, units
from nx_mcp.kit import sketch as ksketch

ENDCAPS = {"offset": "OffsetExact", "translate": "Translation"}
PathRef = Annotated[str, RefTo("curve", "edge")]
TOLERANCE_MM = 0.01


def _check_section(tx, sk, targets) -> None:
    """Refuse, before NX, a section with no closed loop and one that does not lie over the
    target faces along the sketch normal (NX's own messages name neither)."""
    from nx_mcp.domains.modeling import features

    try:
        profile = features._profile(tx, sk)
    except Exception:  # noqa: BLE001 - curves UF cannot sample: NX judges the section
        return
    if not profile:
        return
    section = features._chains(tx, profile)
    if not section["closed"]:
        fail(
            "NX_WRONG_GEOMETRY",
            f"The section has no closed loop ({section['open'] + section['branched']} open "
            "chain(s)): an emboss needs a closed section",
            field="/sketch",
            hint="Close the loop (nx_get_sketch lists the curve ends), or make leftover curves "
            "construction",
            details={k: section[k] for k in ("closed", "open", "branched")},
        )
    frame = ksketch.frame(sk)
    tol = units.part_tolerance(tx)
    drawn = [ksketch.to_local(frame, p) for _c, points in profile for p in points]
    try:
        low, high = measure.bounds(tx, targets)
    except Exception:  # noqa: BLE001 - faces without a box: NX judges the overlap
        return
    corners = [[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1])
               for z in (low[2], high[2])]  # fmt: skip
    seen = [ksketch.to_local(frame, p) for p in corners]
    apart = any(
        min(p[k] for p in drawn) > max(p[k] for p in seen) + tol
        or max(p[k] for p in drawn) < min(p[k] for p in seen) - tol
        for k in (0, 1)
    )
    if apart:
        fail(
            "NX_WRONG_GEOMETRY",
            "The section does not lie over the target faces along the sketch normal",
            field="/sketch",
            hint="Sketch the section over the faces (seen along the sketch normal), or pick "
            "the faces under it",
        )


def _check_path(tx, sheet, curves) -> None:
    """Refuse, before NX, a path curve that does not lie on the sheet."""
    tol = max(10 * units.part_tolerance(tx), units.part_tolerance(tx, TOLERANCE_MM))
    for i, c in enumerate(curves):
        try:
            gaps = [
                curve.point_distance(tx, sheet, curve.point_at(tx, c, t))[0]
                for t in (0.0, 0.5, 1.0)
            ]
        except Exception:  # noqa: BLE001 - no distance: NX judges the path
            continue
        if max(gaps) > tol:
            fail(
                "NX_WRONG_GEOMETRY",
                f"path[{i}] lies up to {max(gaps):.6g} off the sheet; the path must lie on it",
                field=f"/path/{i}",
                hint="Project the curve onto the sheet first (nx_create_projected_curve) and "
                "pass the projection",
                details={"distance": max(gaps)},
            )


class EmbossData(common.FeatureData):
    added: float  # volume change of the body: positive for a pad, negative for a pocket


@tool(
    effect="model",
    label="Emboss section",
    summary="Emboss {sketch} {side} {height}",
    keywords=("emboss", "pad", "pocket", "raised pad", "boss on face", "recess"),
    requires=("work_part", "no_active_sketch"),
    variants="side",
    cost="heavy",
    previewable=True,
    example={"sketch": "s:5", "faces": ["fc:4"], "side": "pad", "height": 5},
)
def nx_create_emboss(
    tx: Tx,
    *,
    sketch: Annotated[SketchRef, Doc("Closed section, sketched off the faces (e.g. above them)")],
    faces: Annotated[list[FaceRef], Items(1, 50), Doc("Target faces of one solid body")],
    side: Annotated[
        Literal["pad", "pocket"], Doc("pad: material raised off the faces; pocket: cut in")
    ] = "pad",
    height: Annotated[Length, Doc("End cap distance from the faces (pad height, pocket depth)")],
    end_cap: Annotated[
        Literal["offset", "translate"],
        Doc("offset: cap follows the faces' curvature; translate: faces moved by height"),
    ] = "offset",
) -> EmbossData:
    """Emboss a closed sketch section onto faces of a solid as a pad or a pocket. The walls
    follow the section along the sketch normal; the end cap lies height off the faces
    (offset keeps the faces' curvature, translate moves them rigidly). volume: the body's;
    added: the change. A tool body: nx_create_emboss_body; a sheet: nx_create_offset_emboss."""
    args.finite(height, "/height", positive=True)
    with tx.checks():
        sk = ksketch.resolve(tx, sketch)
        targets = select.faces(tx, faces, "/faces")
        body = common.one_body(targets, "/faces")
        if not getattr(body, "IsSolidBody", True):
            fail("NX_WRONG_GEOMETRY", "Emboss works on a solid body", field="/faces",
                 hint="For a sheet use nx_create_offset_emboss")  # fmt: skip
        _check_section(tx, sk, targets)
    before = measure.volume(tx, body)
    F = tx.nx.Features
    distance = expr.rhs(height) if side == "pad" else f"-({expr.rhs(height)})"
    with builders.build(tx, tx.part.Features.CreateEmbossBuilder, None) as b:
        b.Tolerance = units.part_tolerance(tx, TOLERANCE_MM)
        region = b.GetRegions()[0]
        region.Section = builders.section(tx, sk)
        convexity = F.EmbossRegion.ConvexityOption
        region.Convexity = convexity.Above if side == "pad" else convexity.Below
        limit = region.GetLimits()[0]
        limit.GetTargets()[0].TargetFace = builders.collector(tx, targets, "Face")
        limit.EndcapOption = getattr(F.EmbossLimit.EndcapMethod, ENDCAPS[end_cap])
        limit.SetDistance(distance)
        feature = b.commit()
    common.sound(tx, [body])
    out = common.result_of(tx, feature, [body])
    common.changed(before, out["volume"], "more" if side == "pad" else "less")
    return {**out, "added": out["volume"] - before}


class OffsetEmbossData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]  # the sheet
    area_before: float  # the sheet's area before
    area: float  # the sheet's area after
    bounds: list[Point3]  # [low, high] of the sheet after


@tool(
    effect="model",
    label="Offset emboss sheet",
    summary="Offset emboss {sheet} along {path} {height}",
    keywords=("offset emboss", "raised strip", "bead", "rib on sheet", "stiffening bead", "groove"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"sheet": "b:6", "path": ["c:9"], "height": 3, "left_width": 5, "side_offset": 2},
)
def nx_create_offset_emboss(
    tx: Tx,
    *,
    sheet: Annotated[BodyRef, Doc("Sheet body to emboss (all its faces)")],
    path: Annotated[list[PathRef], Items(1, 50), Doc("Connected curves/edges lying on the sheet")],
    height: Annotated[Length, Doc("Offset of the strip from the sheet")],
    left_width: Annotated[Length, Doc("Footprint width left of the path")],
    right_width: Annotated[Length, Doc("Right of the path; default left_width")] | None = None,
    side_offset: Annotated[
        Length, Doc("How far the sloped walls run in from the footprint edge; 0: vertical")
    ] = 0.0,
    groove: Annotated[bool, Doc("Press the strip in against the sheet normal")] = False,
) -> OffsetEmbossData:
    """Raise a strip (bead) on a sheet body along curves lying on it, height along the sheet
    normal (groove: against it). Its footprint spans left_width and right_width either
    side of the path; the walls slope in by side_offset, so the top is that much narrower
    on each side and at the ends (0: vertical walls). Keeps the sheet. For a solid use
    nx_create_emboss."""
    args.finite(height, "/height", positive=True)
    args.finite(left_width, "/left_width", positive=True)
    right = left_width if right_width is None else right_width
    args.finite(right, "/right_width", positive=True)
    if float(side_offset) < 0:
        fail("NX_INVALID_ARGUMENT", "side_offset must be 0 or more", field="/side_offset")
    args.distinct("/path", path, "path references")
    with tx.checks():
        body = select.one(tx, sheet, "body", field="/sheet")
        if not getattr(body, "IsSheetBody", False):
            fail("NX_WRONG_GEOMETRY", "Offset emboss works on a sheet body", field="/sheet",
                 hint="For a solid use nx_create_emboss")  # fmt: skip
        curves = [
            select.one(tx, ref, "curve", "edge", field=f"/path/{i}") for i, ref in enumerate(path)
        ]
        _check_path(tx, body, curves)
    faces = list(body.GetFaces())
    area_before = measure.area(tx, faces)
    with builders.build(tx, tx.part.Features.CreateOffsetEmbossBuilder, None) as b:
        b.DistanceTolerance = units.part_tolerance(tx, TOLERANCE_MM)
        b.AngleTolerance = 0.5
        b.Type = tx.nx.Features.OffsetEmbossBuilder.Types.Curve
        builders.fill(tx, b.Target, faces, "Face")
        builders.curve_section(tx, curves, into=b.CenterLine)
        expr.drive(b.HeightOffset, height)
        expr.drive(b.LeftWidth, left_width)
        expr.drive(b.RightWidth, right)
        expr.drive(b.SideOffset, side_offset)
        b.ReverseDirection = bool(groove)
        feature = b.commit()
    made = common.feature_bodies(feature) or [body]
    readback.expect(len(made) == 1, "bodies", f"NX made {len(made)} bodies", bodies=len(made))
    after = list(made[0].GetFaces())
    area = measure.area(tx, after)
    readback.expect(len(after) > len(faces) and not common.same_volume(area_before, area),
                    "area", "The sheet did not change", area=area, before=area_before)  # fmt: skip
    low, high = measure.bounds(tx, made)
    return {
        "feature": tx.ref(feature, kind="feature"),
        "bodies": common.body_rows(tx, made),
        "area_before": area_before,
        "area": area,
        "bounds": [low, high],
    }

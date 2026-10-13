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
from nx_mcp.kit import args, builders, expr, measure, readback, result, select, units
from nx_mcp.kit import sketch as ksketch

ENDCAPS = {"offset": "OffsetExact", "translate": "Translation"}
PathRef = Annotated[str, RefTo("curve", "edge")]
TOLERANCE_MM = 0.01


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
    left_width: Annotated[Length, Doc("Strip width left of the path")],
    right_width: Annotated[Length, Doc("Right of the path; default left_width")] | None = None,
    side_offset: Annotated[Length, Doc("Run of the sloped walls past the strip; 0: steep")] = 0.0,
    groove: Annotated[bool, Doc("Press the strip in against the sheet normal")] = False,
) -> OffsetEmbossData:
    """Raise a strip (bead) on a sheet body along curves lying on it, offset height along the
    sheet normal (groove: into it). The strip spans left_width and right_width either side
    of the path; side_offset is how far its sloped walls run out. Keeps the sheet. For a
    solid use nx_create_emboss."""
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

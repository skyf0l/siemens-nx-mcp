"""Sew, thicken and trim sheet bodies.

NX 2406 lessons kept here: a native refusal (sheets that do not touch within
tolerance, 4635012; a trim boundary that misses the sheet, 671228) is
``NX_BUILD_FAILED`` with ``nx_code``. Sew can leave bodies unsewn or fall back to
a sheet when a solid was asked for; both are ``NX_INCOMPLETE_RESULT`` and roll
back. Tolerances default to 0.001 mm in part units (inch parts scale). A sew or
trim refusal gets a hint and the measured gaps in ``details`` (``gaps``: each tool's
distance to the other sheets; ``region_gap`` and ``boundary_gaps`` for a trim).

Trim Sheet projects its boundaries along the face normals (NX's default,
``ProjectionOptions.DirectionType.FaceNormal``) unless ``direction`` is given
(``Vector``); a region point off the sheet is moved to its nearest sheet point.
"""

from __future__ import annotations

from typing import Annotated, Literal

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    BodyRef,
    Combine,
    Doc,
    FaceRef,
    Items,
    Length,
    Point3,
    RefTo,
    Vec3,
)
from nx_mcp.domains.freeform import common, surface_loops
from nx_mcp.kit import args, builders, curve, facts, geom, measure, readback, select, units
from nx_mcp.kit import bodies as kbodies

# ---------------------------------------------------------------------------
# Sew


class SewData(common.SheetData):
    solid: bool
    volume: float | None
    free_edges: int  # open boundary edges left on the result (0: closed)
    closed: bool  # no open edge left (a closed sheet or a solid)


def _sew_gaps(tx, body, others, refs):
    """``[{tool, gap}]``: each tool sheet's distance to the target and the other tools."""
    out = []
    for i, tool_body in enumerate(others[: common.MAX_DIAGNOSED]):
        near = [common.gap(tx, tool_body, x) for x in [body, *others] if x is not tool_body]
        found = [g for g in near if g is not None]
        out.append({"tool": refs[i], "gap": min(found) if found else None})
    return out


def _sew_failure(error, gaps, tol):
    apart = [g for g in gaps if g["gap"] is not None and g["gap"] > tol]
    if apart:
        worst = max(apart, key=lambda g: g["gap"])
        hint = (
            f"Tool {worst['tool']} is {worst['gap']:.4g} from the other sheets (tolerance "
            f"{tol:.4g}): sheets must share edges; move them together or raise tolerance "
            "above the gap"
        )
    else:
        hint = (
            "The sheets touch but NX found no shared boundary to sew: their edges must "
            "coincide within tolerance (not overlap or cross); raise tolerance slightly"
        )
    return common.explain(error, hint, gaps=gaps, tolerance=tol)


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
    target: BodyRef,
    tools: Annotated[list[BodyRef], Items(1, 100)],
    tolerance: Annotated[Length, Doc("Largest gap to close")] | None = None,
    solid: Annotated[bool, Doc("Require a closed solid result")] = False,
) -> SewData:
    """Sew edge-sharing sheet bodies into the target sheet (tools are consumed). solid=true
    requires a closed solid. Returns free_edges and closed (a closed sheet: sew with solid
    true for a solid). Failures report each tool's gap in details.gaps."""
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
    try:
        with builders.build(tx, part.Features.CreateSewBuilder, None) as b:
            b.Type = F.SewBuilder.Types.Sheet
            b.BodyPreference = getattr(
                F.SewBuilder.BodyPreferenceTypes, "Solid" if solid else "Sheet"
            )
            b.Tolerance = tol
            builders.fill(tx, b.TargetBodiesCollector, [body], "Body")
            builders.fill(tx, b.ToolBodiesCollector, others, "Body")
            feature = b.commit()
            unsewn = list(b.GetUnsewnBodies() or [])
    except NXError as error:
        if error.code != "NX_BUILD_FAILED":
            raise
        raise _sew_failure(error, _sew_gaps(tx, body, others, tools), tol) from None
    if unsewn:
        incomplete = NXError(
            "NX_INCOMPLETE_RESULT",
            f"{len(unsewn)} bodies could not be sewn; the sew was rolled back",
            details={"unsewn": len(unsewn)},
        )
        raise _sew_failure(incomplete, _sew_gaps(tx, body, others, tools), tol)
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
    free = len(surface_loops.free_edges(bodies))
    if not closed and free == 0:
        tx.warn("NX_NOTE", "The sewn sheet is closed (no free edge): sew again with solid "
                "true for a solid body")  # fmt: skip
    return {**out, "solid": closed, "volume": volume, "free_edges": free, "closed": free == 0}


# ---------------------------------------------------------------------------
# Thicken


class ThickenData(common.SheetData):
    volume: float
    side: Literal["along_normal", "against_normal", "both_sides"]
    volume_before: float | None  # combine: the targets' volume before


# NX 2406 "Tool body completely outside target body.": the thickened solid misses a target.
OUTSIDE_TARGET = 670030
GROWS = {"unite": 1.0, "subtract": -1.0, "intersect": -1.0}


def _targets(tx, combine):
    """The solid target bodies of ``combine`` (``[]`` without it)."""
    if combine is None:
        return []
    refs = combine["targets"]
    if combine["operation"] == "unite" and len(refs) > 1:
        fail("NX_INVALID_ARGUMENT", "unite takes one target body", field="/combine/targets",
             hint="Unite the targets first (nx_create_boolean), then pass the result")  # fmt: skip
    found = select.bodies(tx, refs, "/combine/targets")
    for i, body in enumerate(found):
        if common.is_sheet(body):
            fail("NX_WRONG_GEOMETRY", "combine targets must be solid bodies",
                 field=f"/combine/targets/{i}")  # fmt: skip
    return found


def _side(first, second):
    if min(first, second) >= 0:
        return "along_normal"
    if max(first, second) <= 0:
        return "against_normal"
    return "both_sides"


def _offsets(thickness, offset1, offset2):
    """``(first, second)``: thickness t is offsets (t, 0); else offset1 and offset2."""
    if thickness is not None:
        if offset1 is not None or offset2 is not None:
            fail(
                "NX_INVALID_ARGUMENT",
                "Send thickness, or offset1 and offset2, not both",
                field="/thickness",
            )
        return float(thickness), 0.0
    if offset1 is None:
        fail(
            "NX_INVALID_ARGUMENT",
            "Send thickness (or offset1)",
            field="/thickness",
            hint="thickness: 2 makes a 2 mm wall along the face normal; -2 goes against it",
        )
    return float(offset1), float(offset2 or 0.0)


@tool(
    effect="model",
    label="Thicken faces",
    summary=lambda a, say: (
        f"Thicken {say('faces')} by "
        + say("thickness" if a.get("thickness") is not None else "offset1")
    ),
    keywords=("thicken", "offset sheet to solid", "give thickness", "solid from surface"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:7"], "thickness": 2},
)
def nx_create_thicken(
    tx: Tx,
    *,
    faces: Annotated[list[FaceRef], Items(1, 200)],
    thickness: Length | None = None,
    offset1: Length | None = None,
    offset2: Length | None = None,
    combine: Annotated[Combine, Doc("Unite, subtract or intersect with solids; default new")]
    | None = None,
) -> ThickenData:
    """Thicken sheet faces into a solid. thickness t means offset1=t, offset2=0; offsets
    are signed along the face normal (1 and -1: 2 thick, centred). side says where it
    went. combine merges it with target solids in the same feature (volume_before: theirs)."""
    first, second = _offsets(thickness, offset1, offset2)
    if geom.close(first, second, 1e-9):
        field = "/thickness" if thickness is not None else "/offset2"
        fail("NX_INVALID_ARGUMENT", "The thickness must not be zero", field=field)
    with tx.checks():
        selected = select.faces(tx, faces, "/faces")
        of_solid = sum(hasattr(f, "GetBody") and not common.is_sheet(f.GetBody()) for f in selected)
        targets = _targets(tx, combine)
    part = tx.part
    before = measure.volume(tx, targets) if targets else None
    with builders.build(tx, part.Features.CreateThickenBuilder, None) as b:
        builders.fill(tx, b.FaceCollector, selected, "Face")
        b.Tolerance = units.part_tolerance(tx)
        b.FirstOffset.RightHandSide = repr(first)
        b.SecondOffset.RightHandSide = repr(second)
        builders.boolean(tx, b.BooleanOperation, combine)
        try:
            feature = b.commit(validate=True)
        except NXError as error:
            if error.nx_code == OUTSIDE_TARGET:
                error.field = "/combine/targets"
                error.hint = "The thickened solid misses a target body: drop it from targets"
            raise
    bodies = [b for b in common.feature_bodies(feature) if not common.is_sheet(b)]
    if not bodies and targets:  # a combined feature may list no body: the targets changed
        live = {int(x.Tag) for x in part.Bodies}
        bodies = [t for t in targets if int(t.Tag) in live]
    readback.expect(bool(bodies), "solid", "NX built no solid")
    out = common.result_of(tx, feature, bodies)
    kbodies.expect_sound(tx, bodies)
    note = kbodies.scale_note(tx, bodies)
    if note:
        tx.warn("NX_UPDATE_WARNING", note)
    if of_solid and combine is None:
        tx.warn("NX_NOTE", f"{of_solid} face(s) belong to a solid: the thickened solid is a "
                "new body and the source solid is unchanged (combine unites it)")  # fmt: skip
    volume = measure.volume(tx, bodies)
    readback.expect(volume > 0, "volume", "The thickened solid has no volume", volume=volume)
    if combine is not None and before is not None:
        grew = GROWS[combine["operation"]] * (volume - before)
        readback.expect(grew > 1e-6 * max(1.0, before),
                        "combine", f"The {combine['operation']} left the targets unchanged",
                        volume=volume, volume_before=before)  # fmt: skip
    elif len(selected) == 1 and select.face_data(tx, selected[0])[0] == select.FACE_PLANE:
        wanted = measure.area(tx, selected) * abs(first - second)
        readback.expect_close(volume, wanted, "volume", tol=1e-6 * max(1.0, wanted))
    return {**out, "volume": volume, "side": _side(first, second), "volume_before": before}


# ---------------------------------------------------------------------------
# Trim sheet


def _trim_failure(tx, error, target, curves, refs, region_gap, vector):
    gaps = [
        {"boundary": refs[i], "gap": common.gap(tx, c, target)}
        for i, c in enumerate(curves[: common.MAX_DIAGNOSED])
    ]
    tol = units.part_tolerance(tx)
    off = [g for g in gaps if g["gap"] is not None and g["gap"] > tol]
    how = "along direction" if vector is not None else "along the face normals"
    if off and vector is None:
        hint = (
            f"Boundary {off[0]['boundary']} lies {off[0]['gap']:.4g} off the sheet; projected "
            "along the face normals it must cross the sheet: move it, or pass direction "
            "(e.g. [0, 0, 1] for curves drawn in an XY plane)"
        )
    else:
        hint = (
            f"Projected {how}, the boundaries must split the sheet (cross it edge to edge "
            "or form a closed loop on it) and region_point must lie in one of the regions"
        )
    if region_gap is not None and region_gap > tol:
        error.field = "/region_point"
    elif off:
        error.field = f"/boundaries/{refs.index(off[0]['boundary'])}"
    return common.explain(
        error,
        hint,
        region_gap=region_gap,
        boundary_gaps=gaps,
        projection="vector" if vector is not None else "face_normal",
    )


BoundaryRef = Annotated[
    str,
    RefTo("curve", "edge", "face", "datum_plane", "body",
          doc="Curve, edge, face, datum plane or sheet body 'c:5'/'e:7'/'fc:9'/'d:2'/'b:4'"),
]  # fmt: skip


def _boundaries(tx, refs, target):
    """``(objects, strings, faces, planes)``: the boundary objects resolved, then grouped
    into curves and edges (one section), faces and bodies (their faces, one collector) and
    datum planes."""
    args.distinct("/boundaries", refs, "boundaries")
    objs, strings, faces, planes = [], [], [], []
    for i, ref in enumerate(refs):
        field = f"/boundaries/{i}"
        obj = select.one(tx, ref, "curve", "edge", "face", "datum_plane", "body", field=field)
        objs.append(obj)
        if facts.is_edge(obj) or not (hasattr(obj, "GetFaces") or hasattr(obj, "GetBody")):
            if hasattr(obj, "Normal") and hasattr(obj, "Origin"):
                planes.append(obj)
            else:
                strings.append(obj)
        elif hasattr(obj, "GetBody"):
            faces.append(obj)
        else:
            if int(obj.Tag) == int(target.Tag):
                fail("NX_INVALID_ARGUMENT", "The sheet cannot trim itself", field=field)
            faces.extend(obj.GetFaces())
    return objs, strings, faces, planes


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
    body: BodyRef,
    boundaries: Annotated[
        list[BoundaryRef], Items(1, 100), Doc("Curves, edges, faces, datum planes or sheets")
    ],
    region_point: Annotated[Point3, Doc("A point on the region to keep or discard")],
    keep: Annotated[bool, Doc("false: discard the region")] = True,
    direction: Annotated[Vec3, Doc("Project boundaries along this vector")] | None = None,
) -> common.SheetData:
    """Trim a sheet body by curves, edges, faces, datum planes or sheets, keeping or
    discarding the region at region_point. Curves project along the face normals, or along
    direction (e.g. [0,0,1] for curves off a curved sheet)."""
    given = [float(v) for v in region_point]
    with tx.checks():
        target = common.sheet(tx, body)
        curves, strings, faces, planes = _boundaries(tx, boundaries, target)
        vector = None if direction is None else geom.unit(direction, "/direction")
    region_gap, point = curve.point_distance(tx, target, given)
    low, high = measure.bounds(tx, target)
    part = tx.part
    F = tx.nx.Features
    try:
        with builders.build(tx, part.Features.CreateTrimsheetBuilder, None) as b:
            b.TargetBodies.Add(target)
            if strings:
                b.BoundaryObjects.Add(builders.curve_section(tx, strings))
            if faces:
                b.BoundaryObjects.Add(builders.collector(tx, faces, "Face"))
            if planes:  # a datum plane added on its own is "Invalid trimming objects"
                datums = part.ScCollectors.CreateCollector()
                datums.ReplaceRules([part.ScRuleFactory.CreateRuleFaceDatum(planes)], False)
                b.BoundaryObjects.Add(datums)
            if vector is not None:
                projection = b.ProjectionDirection
                methods = tx.nx.GeometricUtilities.ProjectionOptions.DirectionType
                projection.ProjectDirectionMethod = methods.Vector
                projection.ProjectVector = builders.direction(tx, vector, point, "/direction")
            options = F.TrimSheetBuilder.KeepDiscardOption
            b.KeepDiscardMethod = options.Keep if keep else options.Discard
            b.Tolerance = units.part_tolerance(tx)
            b.OutputExactGeometry = True
            b.Regions.Append(part.CreateRegionPoint(builders.point(tx, point), target))
            feature = b.commit()
    except NXError as error:
        if error.code != "NX_BUILD_FAILED":
            raise
        raise _trim_failure(tx, error, target, curves, boundaries, region_gap, vector) from None
    bodies = common.feature_bodies(feature) or [target]
    out = common.result_of(tx, feature, bodies)
    before = 0.0  # the region point lies on the target
    after = min(curve.point_distance(tx, x, point)[0] for x in bodies)
    tol = max(units.part_tolerance(tx), geom.tol(low, high))
    if keep:
        readback.expect(after <= before + tol, "kept_region", before=before, after=after)
    else:
        readback.expect(after > before + tol, "discarded_region", before=before, after=after)
    return out

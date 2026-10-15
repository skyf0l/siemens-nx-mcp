"""Fill Hole (close a sheet opening) and Refit Face (rebuild a B-surface face smoother).

NX 2406 lessons kept here (probed live):

- ``FreeformSurfaceCollection.CreateFillHoleBuilder`` (the modeling one, not the facet
  body one) needs ``solid_modeling`` and its option setters ``nx_freeform_1``; both
  are asked first. The opening goes into its own ``CurveChain`` section.
  ``DefaultEdgeContinuity`` is G1 and is NOT applied to edges added after it is set:
  every border needs a ``BorderContinuity`` item (``SetBorderTypeItems``), else NX
  keeps G1 and a square rim of a ruled tube fails "Some edge cannot have G1
  continuity" (4015907). With G0 items the 20 x 20 rim gives a 400 mm^2 face.
  ``Patch`` false makes a separate sheet; true sews the patch into the rim's sheet
  (the tube: 4 -> 5 faces, free edges 8 -> 4); the feature then returns that sheet.
  Filling the second end too leaves no free edge and NX makes the sheet a solid
  (6 faces, 3200 mm^2). A G1 fill of a round tube's rim (r 10) is a dome reaching
  z + 15.7, 0.0017 off the rim; the sewn rim edges are consumed, so the rim is
  sampled before the build.
- ``CreateRefitFaceBuilder`` (``solid_modeling``) refits one B-surface face in place
  (a REFIT_SURFACE feature on the same body) within ``RefitControl.Tolerance``; a
  planar face is "Invalid face" (670054). The degree and patch setters of
  ``RefitControl.DegreesAndSegmentsOrPatches`` need an advanced freeform license
  (even their getters refuse without it), so NX's defaults are kept. NX 2406's Python
  UF has no ``AskBsurf`` (only the ``ModlBsurface_Struct`` type), so poles and degrees
  cannot be read back: the change is measured (sample deviation, area before/after).
- Not exposed (their type and option setters refuse with 949895 without an advanced
  freeform or Studio license, and NX's defaults alone build nothing useful): Section
  Surface (``CreateSectionSurfaceBuilder``/``Ex``: conic type and spine), Variable
  Offset Face (``CreateVarOffsetFaceBuilder``), Extension (``CreateExtensionBuilder``:
  circular/percentage) and Fit Surface (``CreateFitSurfaceBuilder``).
- A native refusal is ``NX_BUILD_FAILED`` with ``nx_code``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, FaceRef, Items, Length, RefTo
from nx_mcp.domains.freeform import common, surface_loops
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import builders, curve, facts, licenses, measure, readback, select, units

MODELING = "solid_modeling"
FREEFORM = "nx_freeform_1"
StringRef = Annotated[str, RefTo("curve", "edge", doc="Curve or edge 'c:7'/'e:21'")]
ANALYTIC = {16: "cylinder", 17: "cone", 18: "sphere", 19: "torus", 22: "plane"}


def _license(tx, *features):
    for feature in features:
        licenses.require(tx, (feature,), f"This NX seat has no {feature} license for this command",
                         strict=True)  # fmt: skip


class FillHoleData(common.SheetData):
    continuity: Literal["g0", "g1"]
    sewn: bool  # the patch was sewn into the rim's sheet
    gap: float  # largest sampled distance from the opening to the result
    free_edges: int  # open boundary edges left on the result
    closed: bool  # no open edge left (a closed sheet or a solid)


@tool(
    effect="model",
    label="Fill hole",
    summary="Fill the opening {boundary}",
    keywords=(
        "fill hole",
        "close opening",
        "patch hole",
        "cap sheet",
        "close sheet",
        "repair surface hole",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="continuity",
    cost="heavy",
    previewable=True,
    example={"boundary": ["e:4", "e:5", "e:6", "e:7"], "continuity": "g0", "sew": True},
)
def nx_create_fill_hole(
    tx: Tx,
    *,
    boundary: Annotated[
        list[StringRef], Items(1, 100), Doc("One closed loop round the opening, any order")
    ],
    continuity: Annotated[
        Literal["g0", "g1"], Doc("g1: tangent to the rim faces (needs a smooth rim edge loop)")
    ] = "g0",
    sew: Annotated[bool, Doc("Sew the patch into the rim's sheet (needs its edges)")] = True,
    tolerance: Annotated[Length, Doc("Fit tolerance")] | None = None,
) -> FillHoleData:
    """Fill an opening in a sheet with a patch, G0 or G1 to the rim faces, and sew it in
    (sew false: a separate sheet). Filling the last opening closes the sheet into a solid
    (free_edges 0). g1 needs a rim without sharp corners. Planar loop of curves:
    nx_create_bounded_plane."""
    if len(set(boundary)) != len(boundary):
        fail("NX_INVALID_ARGUMENT", "A boundary curve is listed twice", field="/boundary")
    with tx.checks():
        tol = common.given_tolerance(tx, tolerance)
        objs = [
            select.one(tx, ref, "curve", "edge", field=f"/boundary/{i}")
            for i, ref in enumerate(boundary)
        ]
        loops = surface_loops.loops_of(tx, objs, surface_loops.chaining(tol))
        if len(loops) != 1:
            fail("NX_INVALID_ARGUMENT", f"The boundary forms {len(loops)} loops: fill one "
                 "opening per call", field="/boundary")  # fmt: skip
        owners = set()
        for i, obj in enumerate(objs):
            faces = list(obj.GetFaces()) if facts.is_edge(obj) else []
            if (sew or continuity == "g1") and len(faces) != 1:
                fail(
                    "NX_WRONG_GEOMETRY",
                    f"{boundary[i]}: sew and g1 need open sheet edges (one face each)",
                    field=f"/boundary/{i}",
                    hint="Pass the sheet's boundary edges, or sew false and g0 for curves",
                )
            if faces:
                owners.add(int(faces[0].GetBody().Tag))
        if sew and len(owners) != 1:
            fail("NX_INVALID_ARGUMENT", "sew needs the edges of one sheet", field="/boundary")
        points = [p for o in objs for p in surface_loops.curve_points(tx, o)]
        rim_sheets = [x for x in tx.part.Bodies if int(x.Tag) in owners]
        open_before = len(surface_loops.free_edges(rim_sheets))
    _license(tx, MODELING, FREEFORM)
    part = tx.part
    H = tx.nx.Features.FillHoleBuilder
    kind = H.ContinuityTypes.G1 if continuity == "g1" else H.ContinuityTypes.G0
    before = {int(b.Tag) for b in part.Bodies}
    collection = part.Features.FreeformSurfaceCollection
    with builders.build(tx, collection.CreateFillHoleBuilder, None) as b:
        b.DefaultEdgeContinuity = kind
        builders.curve_section(tx, objs, into=b.CurveChain)
        b.Tolerance = tol
        b.Patch = bool(sew)
        items = []
        for obj in objs:
            item = H.BorderContinuity()
            item.BorderObject = obj
            item.Continuity = kind
            items.append(item)
        b.SetBorderTypeItems(items)
        feature = b.commit()
    made = common.feature_bodies(feature)
    if not made:
        made = [x for x in part.Bodies if int(x.Tag) not in before]
    out = common.result_of(tx, feature, made)
    kbodies.expect_sound(tx, made)
    gap = surface_loops.points_gap(tx, points, made)  # sewn rim edges are gone now
    limit = 2 * tol + 10 * units.part_tolerance(tx)
    readback.expect(gap <= limit, "boundary", "The patch misses the opening", gap=gap)
    free = surface_loops.free_edges(made)
    if sew:
        readback.expect(len(free) < open_before, "sewn", "The patch was not sewn into the "
                        "sheet", free_edges=len(free), before=open_before)  # fmt: skip
    return {**out, "continuity": continuity, "sewn": bool(sew), "gap": gap,
            "free_edges": len(free), "closed": not free}  # fmt: skip


class RefitData(common.SheetData):
    tolerance: float
    deviation: float  # largest distance from the original face's sample points to the refit
    samples: int
    area_before: float  # the original face's area (area: the refit body's faces)
    changed: bool  # the refit face moved off the original samples or changed its area


@tool(
    effect="model",
    label="Refit face",
    summary="Refit {face} within {tolerance}",
    keywords=(
        "refit face",
        "smooth face",
        "rebuild surface",
        "simplify surface",
        "fair surface",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"face": "fc:7", "tolerance": 0.01},
)
def nx_create_refit_face(
    tx: Tx,
    *,
    face: Annotated[FaceRef, Doc("A B-surface face (not a plane, cylinder, cone...)")],
    tolerance: Annotated[Length, Doc("Largest allowed deviation from the face")] = 0.01,
) -> RefitData:
    """Refit a freeform (B-surface) face in place within a tolerance to smooth or simplify
    it (imported or wavy faces). NX's default degrees and patches are used (not settable);
    the face keeps its edges. Returns deviation (original samples to the refit face),
    area_before and changed."""
    tol = float(tolerance)
    if not tol > 0:
        fail("NX_INVALID_ARGUMENT", "tolerance must be positive", field="/tolerance")
    with tx.checks():
        target = select.one(tx, face, "face", field="/face")
        kind = select.face_data(tx, target)[0]
        if kind in ANALYTIC:
            fail("NX_WRONG_GEOMETRY", f"Refit needs a B-surface face, not a {ANALYTIC[kind]}",
                 field="/face")  # fmt: skip
        points = [p for p, _n in facts.interior_samples(tx, target, steps=5)]
        points += [curve.point_at(tx, e, 0.5) for e in target.GetEdges()]
        body = target.GetBody()
        area_before = measure.area(tx, [target])
    _license(tx, MODELING)
    with builders.build(tx, tx.part.Features.CreateRefitFaceBuilder, None) as b:
        b.Face.Value = target
        b.RefitControl.Tolerance = tol
        feature = b.commit()
    made = common.feature_bodies(feature) or [body]
    out = common.result_of(tx, feature, made)
    kbodies.expect_sound(tx, made)
    faces = [f for x in made for f in x.GetFaces()]  # faces, not bodies: a solid has inside
    deviation = max((min(curve.point_distance(tx, f, p)[0] for f in faces) for p in points),
                    default=0.0)  # fmt: skip
    limit = tol + 10 * units.part_tolerance(tx)
    readback.expect(deviation <= limit, "deviation", "The refit face leaves the tolerance",
                    deviation=deviation)  # fmt: skip
    middle = points[len(points) // 2] if points else None
    refit = min(faces, key=lambda f: curve.point_distance(tx, f, middle)[0]) if middle else None
    area_after = measure.area(tx, [refit]) if refit is not None else area_before
    changed = deviation > units.part_tolerance(tx, 1e-6) or (
        abs(area_after - area_before) > 1e-6 * max(1.0, area_before)
    )
    if not changed:
        tx.warn("NX_NOTE", "NX kept the face's shape within the tolerance (same points and "
                "area): Refit Face uses NX's default degrees and patches")  # fmt: skip
    return {**out, "tolerance": tol, "deviation": deviation, "samples": len(points),
            "area_before": area_before, "changed": bool(changed)}  # fmt: skip

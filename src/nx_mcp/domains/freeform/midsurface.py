"""Midsurface: the mid sheet of a thin solid (auto or by face pairs) or between two sheets.

NX 2406 lessons kept here (probed live on a 40 x 20 x 4 plate at z 0..4):

- The three builders need Modeling's license (``solid_modeling``), asked first.
- ``CreateMidSurfaceBuilder`` pairs faces itself: its ``FacePairing.InputSolidBodies``
  collector takes the solid (progressive pairing, the default); the plate gives one
  800 mm^2 face at z 2. The solid stays (not hidden).
- ``CreateMidSurfaceByFacePairsBuilder``: ``BodySelection.Add(solid)``, the side one
  and side two face collectors, then ``CreateFacePair()`` before the commit (the same
  800 mm^2 sheet at z 2).
- ``CreateMidSurfaceByTwoSheetsBuilder``: ``FirstSheetBody``/``SecondSheetBody``
  collectors; two parallel 20 x 20 sheets 30 apart give a 400 mm^2 sheet at 15.
- A native refusal is ``NX_BUILD_FAILED`` with ``nx_code``.

Read back: points inside each mid face are measured to both sides (face pairs, two
sheets) or to the nearest face of the solid (auto): ``half_thickness`` is their range
and ``asymmetry`` the largest side-to-side difference.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Doc, FaceRef, Items
from nx_mcp.domains.freeform import common
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import builders, curve, facts, licenses, readback, select, units

LICENSE = "solid_modeling"
ASYMMETRY = 0.01  # share of the local thickness the mid sheet may lean to one side


class Range(TypedDict):
    min: float
    max: float


class MidsurfaceData(common.SheetData):
    method: Literal["auto", "face_pairs", "two_sheets"]
    half_thickness: Range  # distance from points of the mid sheet to the sides
    asymmetry: float | None  # face_pairs/two_sheets: largest |side one - side two| distance
    samples: int


def _solid(tx, ref):
    body = select.one(tx, ref, "body", field="/body")
    if common.is_sheet(body):
        fail("NX_WRONG_GEOMETRY", "Select a solid body; for two sheets use method two_sheets",
             field="/body")  # fmt: skip
    return body


def _side(tx, refs, body, field):
    faces = select.faces(tx, refs, field)
    for i, f in enumerate(faces):
        if int(f.GetBody().Tag) != int(body.Tag):
            fail("NX_INVALID_ARGUMENT", "The face is not on body", field=f"{field}/{i}")
    return faces


def _nearest(tx, targets, point):
    return min(curve.point_distance(tx, t, point)[0] for t in targets)


def _measure(tx, bodies, sides):
    """``(half range, asymmetry or None, samples)`` from interior points of the mid
    faces to each side (one side: the nearest of its faces)."""
    lows: list[float] = []
    worst = None
    count = 0
    for body in bodies:
        for face in body.GetFaces():
            tx.checkpoint()
            for point, _normal in facts.interior_samples(tx, face):
                gaps = [_nearest(tx, side, point) for side in sides]
                lows.append(min(gaps))
                count += 1
                if len(gaps) == 2:
                    lean = abs(gaps[0] - gaps[1])
                    worst = lean if worst is None else max(worst, lean)
    readback.expect(count > 0, "samples", "No point of the mid sheet could be measured")
    return {"min": min(lows), "max": max(lows)}, worst, count


@tool(
    effect="model",
    label="Create midsurface",
    summary=lambda a, say: (
        "Midsurface "
        + (f"between {say('sheets')}" if a.get("method") == "two_sheets" else f"of {say('body')}")
    ),
    keywords=(
        "midsurface",
        "mid sheet",
        "mid plane",
        "neutral surface",
        "shell mesh surface",
        "mid surface between sheets",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="method",
    required_by={
        "auto": ("body",),
        "face_pairs": ("body", "side_one", "side_two"),
        "two_sheets": ("sheets",),
    },
    cost="heavy",
    previewable=True,
    example={"body": "b:3", "method": "auto"},
)
def nx_create_midsurface(
    tx: Tx,
    *,
    method: Annotated[
        Literal["auto", "face_pairs", "two_sheets"],
        Doc("auto: NX pairs the solid's faces; face_pairs: given sides; two_sheets: sheets"),
    ] = "auto",
    body: Annotated[BodyRef, Doc("auto/face_pairs: the thin solid (kept)")] | None = None,
    side_one: Annotated[list[FaceRef], Items(1, 100), Doc("face_pairs: faces of one side")]
    | None = None,
    side_two: Annotated[list[FaceRef], Items(1, 100), Doc("face_pairs: the opposite faces")]
    | None = None,
    sheets: Annotated[list[BodyRef], Items(2, 2), Doc("two_sheets: the two sheet bodies")]
    | None = None,
) -> MidsurfaceData:
    """Create the mid sheet of a thin solid or between two sheets (plate, rib, hull skin
    for shell meshing). auto pairs the solid's faces; face_pairs uses the given opposite
    faces. Reports half_thickness (mid sheet to the sides) and asymmetry."""
    used = {"body": body, "side_one": side_one, "side_two": side_two, "sheets": sheets}
    wanted = {"auto": ("body",), "face_pairs": ("body", "side_one", "side_two"),
              "two_sheets": ("sheets",)}[method]  # fmt: skip
    for name, value in used.items():
        if value is not None and name not in wanted:
            fail("NX_INVALID_ARGUMENT", f"{name} is not used by method {method}",
                 field=f"/{name}")  # fmt: skip
        if value is None and name in wanted:
            fail("NX_INVALID_ARGUMENT", f"method {method} needs {name}", field=f"/{name}")
    with tx.checks():
        solid = None if body is None else _solid(tx, body)
        firsts: list = []
        seconds: list = []
        pair: list = []
        if method == "face_pairs":
            firsts = _side(tx, side_one, solid, "/side_one")
            seconds = _side(tx, side_two, solid, "/side_two")
            if {int(f.Tag) for f in firsts} & {int(f.Tag) for f in seconds}:
                fail("NX_INVALID_ARGUMENT", "A face is on both sides", field="/side_two")
        if method == "two_sheets":
            pair = common.sheets(tx, sheets, "/sheets")
            if int(pair[0].Tag) == int(pair[1].Tag):
                fail("NX_INVALID_ARGUMENT", "Give two different sheets", field="/sheets/1")
    licenses.require(tx, (LICENSE,), f"This NX seat has no {LICENSE} license for this command")
    part = tx.part
    before = {int(x.Tag) for x in part.Bodies}
    if method == "auto":
        with builders.build(tx, part.Features.CreateMidSurfaceBuilder, None) as b:
            builders.fill(tx, b.FacePairing.InputSolidBodies, [solid], "Body", sheets=True)
            b.DistanceTolerance = units.part_tolerance(tx)
            feature = b.commit()
        sides = [list(solid.GetFaces())] if solid is not None else []  # auto needs body
    elif method == "face_pairs":
        with builders.build(tx, part.Features.CreateMidSurfaceByFacePairsBuilder, None) as b:
            b.BodySelection.Add(solid)
            builders.fill(tx, b.SideOneSelection, firsts, "Face")
            builders.fill(tx, b.SideTwoSelection, seconds, "Face")
            b.CreateFacePair()
            feature = b.commit()
        sides = [firsts, seconds]
    else:
        with builders.build(tx, part.Features.CreateMidSurfaceByTwoSheetsBuilder, None) as b:
            builders.fill(tx, b.FirstSheetBody, [pair[0]], "Body", sheets=True)
            builders.fill(tx, b.SecondSheetBody, [pair[1]], "Body", sheets=True)
            feature = b.commit()
        sides = [[pair[0]], [pair[1]]]
    # NX 2406: the face-pairs feature lists no body; its sheet is the part's new body.
    made = common.feature_bodies(feature) or [x for x in part.Bodies if int(x.Tag) not in before]
    out = common.result_of(tx, feature, made)
    readback.expect(out["sheet"], "sheet", "NX built no mid sheet")
    kbodies.expect_sound(tx, made)
    half, lean, count = _measure(tx, made, sides)
    limit = 10 * units.part_tolerance(tx)
    if lean is not None and lean > limit + ASYMMETRY * 2 * half["max"]:
        tx.warn("NX_APPROXIMATED", f"The mid sheet leans {lean:.4g} towards one side: check "
                "the face pairs (nx_measure_distance)")  # fmt: skip
    return {**out, "method": method, "half_thickness": half, "asymmetry": lean,
            "samples": count}  # fmt: skip

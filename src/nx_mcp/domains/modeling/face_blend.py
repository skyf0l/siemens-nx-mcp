"""Face blend: a rolling-ball blend between two face sets (tool-surface 3.6).

NX 2406 lessons kept here: ``FaceBlendBuilder`` takes the two walls in
``FirstFaceCollector``/``SecondFaceCollector`` and a constant radius in
``CircularCrossSection.Radius``; the ball rolls on the side both face normals point to
(``ReverseFirstFaceNormal``/``ReverseSecondFaceNormal`` flip a wall).
``TrimInputFacesToBlendFaces`` with ``SewAllFaces`` trims the walls to the blend and
sews one body; without them NX adds the blend as a separate sheet. A rolling-ball blend
is a canal surface: one principal curvature is 1/radius everywhere, which the read-back
samples.
"""

from __future__ import annotations

import math
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, FaceRef, Items, Length
from nx_mcp.domains.modeling import common
from nx_mcp.kit import builders, expr, facts, measure, readback, result, select, surface, units

FaceList = Annotated[list[FaceRef], Items(1, 100)]
FLIP_HINT = (
    "The ball rolls on the side both face normals point to (outward on a solid): a concave "
    "corner takes flip1/flip2 false, a convex edge both true; check the radius fits the walls"
)
# Relative tolerance of the measured blend radius.
RADIUS_REL = 1e-3


class FaceBlendData(TypedDict):
    feature: result.Row
    bodies: list[result.Row]
    volume: float  # solid result bodies' total (0 for sheets)
    area: float  # the blend faces' total area
    blend_faces: list[result.Row]
    radius: float  # measured on the blend faces


def _principal(shape) -> tuple[float, float]:
    """The two principal curvatures of a 3x3 shape operator (one eigenvalue is 0)."""
    trace = sum(shape[i][i] for i in range(3))
    squares = sum(shape[i][j] ** 2 for i in range(3) for j in range(3))
    # k1 + k2 = trace and k1^2 + k2^2 = squares
    spread = math.sqrt(max(0.0, 2.0 * squares - trace * trace))
    return (trace + spread) / 2.0, (trace - spread) / 2.0


def measured_radius(tx, faces, want: float) -> float:
    """The blend radius read from the faces: a cylinder's radius, else the principal
    curvature radius nearest ``want`` at interior samples (a rolling ball's section)."""
    found = []
    for face in faces:
        kind, _, _ = select.face_data(tx, face)
        if kind == select.FACE_CYLINDER:
            found.append(float(tx.uf.Modeling.AskFaceData(face.Tag)[4]))
            continue
        for point, _ in facts.interior_samples(tx, face):
            try:
                _, shape = surface.differential(tx, face, point)
            except NXError:
                continue
            radii = [1.0 / abs(k) for k in _principal(shape) if abs(k) > 1e-12]
            if radii:
                found.append(min(radii, key=lambda r: abs(r - want)))
    readback.expect(bool(found), "radius", "No blend face could be measured")
    return max(found, key=lambda r: abs(r - want))


TWO_SOLIDS_HINT = (
    "Faces of two separate solids cannot be trimmed and sewn into one: unite the bodies "
    "first (nx_create_boolean), or pass trim false for a separate blend sheet"
)


def _one_solid(first, second) -> None:
    """Refuse trim with the walls on two different solids: NX 2406 fails "Unable to attach
    Blends" (sheets of different bodies sew, solids do not)."""
    a, b = first[0].GetBody(), second[0].GetBody()
    both_solid = getattr(a, "IsSolidBody", True) and getattr(b, "IsSolidBody", True)
    if both_solid and int(a.Tag) != int(b.Tag):
        fail(
            "NX_INVALID_ARGUMENT",
            "faces1 and faces2 lie on two separate solid bodies: a trimmed face blend needs "
            "one solid",
            field="/faces2",
            hint=TWO_SOLIDS_HINT,
        )


@tool(
    effect="model",
    label="Blend between face sets",
    summary="Face blend {faces1} to {faces2} R{radius}",
    keywords=(
        "face blend",
        "fillet between faces",
        "rolling ball",
        "hull deck fillet",
        "stringer fillet",
        "blend sheets",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"faces1": ["fc:4"], "faces2": ["fc:9"], "radius": 5},
)
def nx_create_face_blend(
    tx: Tx,
    *,
    faces1: Annotated[FaceList, Doc("First wall's faces (one body or sheet)")],
    faces2: Annotated[FaceList, Doc("Second wall's faces; another solid only with trim false")],
    radius: Annotated[Length, Doc("Rolling ball radius, > 0")],
    flip1: Annotated[bool, Doc("Roll the ball on the other side of faces1")] = False,
    flip2: Annotated[bool, Doc("Roll the ball on the other side of faces2")] = False,
    trim: Annotated[
        bool, Doc("Trim the walls to the blend and sew; false adds a lone sheet")
    ] = True,
) -> FaceBlendData:
    """Blend two face sets with a rolling ball of a constant radius (hull-deck, stringer
    fillets). The ball rolls where both face normals point: outward on a solid, so a
    concave corner fills; flip1 and flip2 true round a convex edge. Sheets: the side their
    normals face. Walls on two solids: unite them first, or trim false. Edges of one body:
    nx_create_blend."""
    want = float(radius)
    if want <= 0:
        fail("NX_INVALID_ARGUMENT", "radius must be positive", field="/radius")
    with tx.checks():
        first = select.faces(tx, faces1, "/faces1")
        second = select.faces(tx, faces2, "/faces2")
        tags = {int(f.Tag) for f in first}
        for i, f in enumerate(second):
            if int(f.Tag) in tags:
                fail("NX_INVALID_ARGUMENT", "A face is in both sets", field=f"/faces2/{i}",
                     hint="Each wall is its own face set")  # fmt: skip
        if trim:
            _one_solid(first, second)
    inputs: list = []
    for f in [*first, *second]:
        body = f.GetBody()
        if int(body.Tag) not in {int(b.Tag) for b in inputs}:
            inputs.append(body)
    solids = [b for b in inputs if getattr(b, "IsSolidBody", True)]
    before = measure.volume(tx, solids) if solids else 0.0
    known = common.body_tags(tx)
    try:
        with builders.build(tx, tx.part.Features.CreateFaceBlendBuilder, None) as b:
            b.Tolerance = units.part_tolerance(tx)
            b.FirstFaceCollector = builders.collector(tx, first, "Face")
            b.SecondFaceCollector = builders.collector(tx, second, "Face")
            b.ReverseFirstFaceNormal = bool(flip1)
            b.ReverseSecondFaceNormal = bool(flip2)
            expr.drive(b.CircularCrossSection.Radius, radius)
            b.TrimInputFacesToBlendFaces = bool(trim)
            b.SewAllFaces = bool(trim)
            feature = b.commit()
    except NXError as error:
        if error.code == "NX_BUILD_FAILED" and not error.hint:
            error.hint = FLIP_HINT
        raise
    alive = {int(x.Tag) for x in tx.part.Bodies}
    made = [x for x in common.feature_bodies(feature) if int(x.Tag) in alive]
    made = made or [x for x in inputs if int(x.Tag) in alive]
    have = {int(x.Tag) for x in made}
    made += [x for x in tx.part.Bodies if int(x.Tag) not in known | have]
    blend = list(feature.GetFaces()) if hasattr(feature, "GetFaces") else []
    readback.expect(bool(blend), "blend_faces", "NX made no blend face")
    got = measured_radius(tx, blend, want)
    readback.expect_close(got, want, "radius", tol=max(RADIUS_REL * want, units.part_tolerance(tx)))
    common.sound(tx, made)
    solid_made = [x for x in made if getattr(x, "IsSolidBody", True)]
    volume = measure.volume(tx, solid_made) if solid_made else 0.0
    if solids and solid_made:
        common.changed(before, volume, "differs")
    return {
        "feature": tx.ref(feature, kind="feature"),
        "bodies": common.body_rows(tx, made),
        "volume": volume,
        "area": measure.area(tx, blend),
        "blend_faces": [tx.ref(f, kind="face") for f in blend],
        "radius": got,
    }

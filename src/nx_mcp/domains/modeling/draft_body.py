"""Draft Body: parting draft on both sides of a parting object (plug and mould halves).

NX 2406 lessons kept here (probed live on a 100 x 60 x 40 and an 80 x 80 x 10 block):

- ``Features.CreateDraftBodyBuilder`` with ``Type`` Faces, ``PartingObject.Value`` a datum
  plane (fixed or a datum plane feature), ``DrawDirection`` a smart direction,
  ``FacesToDraft`` a face collector and ``DraftAngle`` an expression; ``Tolerance``
  defaults to 0 and is set to the part tolerance. The feature is ``BODY_TAPER`` with one
  expression ("Draft Body(n) Draw Angle") and keeps the body.
- Every selected face that crosses the parting splits there: the part above tapers
  against the draw direction, the part below along it, and the parting outline grows
  outward while the faces' far edges stay put (4 side faces at 5 deg about z 20 on the
  100 x 60 x 40 block: 8 faces at exactly 5 deg, volume 240000 -> 251361.84). Reversing the
  draw direction gives the same body.
- Match: on faces already sloped (the 5 deg block drafted again at 8 deg about z 10) the
  two halves do not meet at the parting. MatchType None leaves a step face there (8 deg and
  90 deg faces, +11862.09); ToIsocline and TangentToFace add a matching face
  (8 and 22.86 deg, +16570.08). On vertical faces all three give the same body. The
  Isocline and TrueDraft methods gave the same bodies on planar faces.
- A zero or negative angle is refused ("Cannot apply draft").
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, Direction, Doc, FaceRef, Items, Plane
from nx_mcp.domains.modeling import common
from nx_mcp.kit import builders, expr, geom, measure, readback, result, select, units

FACE_PLANE = 22
# Read-back tolerance of a drafted planar face's angle, degrees.
ANGLE_TOL = 0.01
MATCH_TYPES = {"none": "NotSet", "isocline": "ToIsocline", "tangent": "TangentToFace"}
METHODS = {"isocline": "Isocline", "true_draft": "TrueDraft"}


class DraftedFace(TypedDict):
    face: result.Row
    angle: float  # degrees between the face and the draw direction


class DraftBodyData(common.FeatureData):
    faces: list[DraftedFace]  # planar faces of the feature with their measured angle


def _parting(tx, value):
    """The parting object: a datum plane or planar face, else a fixed datum plane."""
    obj, origin, normal = select.plane(tx, common.principal_plane(value), "/parting")
    return obj if obj is not None else builders.fixed_plane(tx, origin, normal)


def _angle(tx, face, d) -> float | None:
    """Degrees between a planar face and the direction ``d``; None for other faces."""
    kind, _point, normal = select.face_data(tx, face)
    if kind != FACE_PLANE:
        return None
    n = geom.unit(normal)
    return math.degrees(math.asin(min(1.0, abs(geom.dot(n, d)))))


@tool(
    effect="model",
    label="Draft body",
    summary="Draft body {faces} {angle} about {parting}",
    keywords=(
        "draft body",
        "parting draft",
        "mould draft",
        "split draft",
        "plug and mould",
        "two-sided draft",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:4", "fc:5"], "parting": "xy", "direction": "z", "angle": 3},
)
def nx_create_draft_body(
    tx: Tx,
    *,
    faces: Annotated[list[FaceRef], Items(1, 200), Doc("Side faces crossing the parting")],
    parting: Annotated[
        Plane, Doc("Parting plane: datum plane, planar face, 'xy' or origin+normal")
    ],
    direction: Annotated[Direction, Doc("Draw direction of the upper half, e.g. 'z'")],
    angle: Annotated[Angle, Doc("Draft angle, 0..89 degrees")],
    match: Annotated[
        Literal["none", "isocline", "tangent"],
        Doc("Halves that miss at the parting: none leaves a step face, else a matching face"),
    ] = "isocline",
    method: Literal["isocline", "true_draft"] = "isocline",
) -> DraftBodyData:
    """Draft faces on both sides of a parting plane (mould split, plug and mould halves). Above
    it they taper against the draw direction, below it along it: the parting outline is the
    widest and the far edges stay put. Faces must cross or touch the parting; a one-sided
    draft about a stationary face: nx_create_draft. faces: planar faces at the angle."""
    a = float(angle)
    if not 0 < a < 89:
        fail("NX_INVALID_ARGUMENT", "angle must be within 0..89 degrees (exclusive)",
             field="/angle", hint="Send e.g. 'angle': 3; both halves get the same angle")  # fmt: skip
    with tx.checks():
        selected = select.faces(tx, faces, "/faces")
        body = common.one_body(selected, "/faces")
        if not getattr(body, "IsSolidBody", True):
            fail("NX_WRONG_GEOMETRY", "Draft Body works on solid bodies", field="/faces")
        d = select.direction(tx, common.principal(direction), "/direction")
        planar = 0
        for i, face in enumerate(selected):
            now = _angle(tx, face, d)
            if now is None:
                continue
            planar += 1
            if now > 89.9:
                fail("NX_WRONG_GEOMETRY", "A face square to the draw direction cannot be drafted",
                     field=f"/faces/{i}", hint="Select the side walls that cross the parting")  # fmt: skip
    before = measure.volume(tx, body)
    B = tx.nx.Features.DraftBodyBuilder
    with builders.build(tx, tx.part.Features.CreateDraftBodyBuilder, None) as b:
        b.Tolerance = units.part_tolerance(tx, 0.01)
        b.Type = B.Types.Faces
        b.PartingObject.Value = _parting(tx, parting)
        b.DrawDirection = builders.direction(tx, d, field="/direction")
        builders.fill(tx, b.FacesToDraft, selected, "Face")
        expr.drive(b.DraftAngle, angle)  # an expression keeps the angle driven
        b.DraftMethod = getattr(B.Method, METHODS[method])
        b.MatchType = getattr(B.MatchingType, MATCH_TYPES[match])
        feature = b.commit()
    common.sound(tx, [body])
    out = common.result_of(tx, feature, [body])
    common.changed(before, out["volume"], "differs")
    drafted: list[DraftedFace] = []
    for face in feature.GetFaces():
        now = _angle(tx, face, d)
        if now is not None and abs(now - a) <= ANGLE_TOL:
            drafted.append({"face": tx.ref(face, kind="face"), "angle": now})
    # Matching faces replace part of the drafted ones (8 deg about a sloped block: 4 faces
    # at 8 deg and 4 matching faces at 22.86 deg): at least one planar face must be at angle.
    readback.expect(
        bool(drafted) or not planar,
        "angle",
        f"No face of the draft lies at {a:g} deg to the draw direction",
        planar_faces=planar,
    )
    return {**out, "faces": drafted}

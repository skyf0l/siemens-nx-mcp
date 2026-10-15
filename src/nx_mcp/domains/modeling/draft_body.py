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

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, Direction, Doc, FaceRef, Items, Plane
from nx_mcp.domains.modeling import common
from nx_mcp.domains.modeling.bodies import plane_range
from nx_mcp.kit import builders, expr, geom, measure, readback, result, select, units

FACE_PLANE = 22
# Read-back tolerance of a drafted planar face's angle, degrees.
ANGLE_TOL = 0.01
MATCH_TYPES = {"none": "NotSet", "isocline": "ToIsocline", "tangent": "TangentToFace"}
METHODS = {"isocline": "Isocline", "true_draft": "TrueDraft"}


class DraftedFace(TypedDict):
    face: result.Row
    angle: float  # degrees between the face and the draw direction
    at_angle: bool  # within 0.01 deg of the requested angle


class DraftBodyData(common.FeatureData):
    faces: list[DraftedFace]  # every planar face of the feature with its measured angle


def _parting(tx, plane):
    """The parting object of ``(obj, origin, normal)``: a datum plane or planar face, else
    a fixed datum plane."""
    obj, origin, normal = plane
    return obj if obj is not None else builders.fixed_plane(tx, origin, normal)


def _angle(tx, face, d) -> float | None:
    """Degrees between a planar face and the direction ``d``; None for other faces."""
    kind, _point, normal = select.face_data(tx, face)
    if kind != FACE_PLANE:
        return None
    return readback.draft_angle(normal, d)


def _crossing(tx, selected, origin, normal) -> None:
    """Refuse faces that all lie wholly on one side of the parting plane (every bounding box
    corner on one side): NX cannot match the halves there, and match 'none' then drafts the
    faces out to the plane, deforming the body. A face on one side next to faces that
    cross is fine (a sloped wall drafted again about a lower parting: NX adds matching
    faces or a step face)."""
    away: list[tuple[float, int, float, float]] = []
    for i, face in enumerate(selected):
        try:
            low, high = measure.bounds(tx, [face])
        except NXError:
            return  # a face NX cannot box: NX decides
        lo, hi = plane_range(low, high, origin, normal)
        tol = geom.tol(low, high, origin)
        if lo <= tol and hi >= -tol:
            return  # this face crosses or touches the parting
        away.append((min(abs(lo), abs(hi)), i, lo, hi))
    if not away:
        return
    _gap, i, lo, hi = min(away)
    fail(
        "NX_WRONG_GEOMETRY",
        f"The faces lie wholly on one side of the parting plane (faces[{i}], the nearest, is "
        f"{lo:.6g}..{hi:.6g} from it): at least one must cross or touch the parting",
        field=f"/faces/{i}",
        hint="Move the parting plane into the faces (e.g. through the parting line), or "
        "draft faces on one side about a stationary face with nx_create_draft",
        details={"face": i, "distances": [lo, hi]},
    )


def _reach(tx, body, d) -> tuple[float, float] | None:
    """The body's extent along a principal draw direction ``d`` (box), None for others."""
    if not any(abs(abs(c) - 1.0) <= 1e-9 for c in d):
        return None
    low, high = measure.bounds(tx, [body])
    return plane_range(low, high, [0.0, 0.0, 0.0], d)


PARTING_HINT = (
    "Move the parting plane so it crosses the faces; match 'none' then drafts the faces out "
    "to the plane and deforms the body"
)


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
    widest and the far edges stay put. At least one face must cross or touch the parting (else refused);
    a one-sided draft about a stationary face: nx_create_draft. faces: every planar face of
    the draft with its angle; at_angle false ones are warned about."""
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
        plane = select.plane(tx, common.principal_plane(parting), "/parting")
        _crossing(tx, selected, plane[1], plane[2])
    before = measure.volume(tx, body)
    reach = _reach(tx, body, d)
    B = tx.nx.Features.DraftBodyBuilder
    try:
        with builders.build(tx, tx.part.Features.CreateDraftBodyBuilder, None) as b:
            b.Tolerance = units.part_tolerance(tx, 0.01)
            b.Type = B.Types.Faces
            b.PartingObject.Value = _parting(tx, plane)
            b.DrawDirection = builders.direction(tx, d, field="/direction")
            builders.fill(tx, b.FacesToDraft, selected, "Face")
            expr.drive(b.DraftAngle, angle)  # an expression keeps the angle driven
            b.DraftMethod = getattr(B.Method, METHODS[method])
            b.MatchType = getattr(B.MatchingType, MATCH_TYPES[match])
            feature = b.commit()
    except NXError as error:
        if "parting" in str(error.message).lower() and not error.hint:
            error.hint = PARTING_HINT  # NX's own advice (match none) deforms the body
            error.field = error.field or "/parting"
        raise
    common.sound(tx, [body])
    out = common.result_of(tx, feature, [body])
    common.changed(before, out["volume"], "differs")
    drafted: list[DraftedFace] = []
    for face in feature.GetFaces():
        now = _angle(tx, face, d)
        if now is not None:
            at = abs(now - a) <= ANGLE_TOL
            drafted.append({"face": tx.ref(face, kind="face"), "angle": now, "at_angle": at})
    # Matching faces replace part of the drafted ones (8 deg about a sloped block: 4 faces
    # at 8 deg and 4 matching faces at 22.86 deg): at least one planar face must be at angle.
    readback.expect(
        any(f["at_angle"] for f in drafted) or not planar,
        "angle",
        f"No face of the draft lies at {a:g} deg to the draw direction",
        planar_faces=planar,
    )
    off = sorted({round(f["angle"], 3) for f in drafted if not f["at_angle"]})
    if off:
        tx.warn(
            "NX_APPROXIMATED",
            f"{sum(not f['at_angle'] for f in drafted)} planar face(s) of the draft lie at "
            f"{', '.join(f'{x:g}' for x in off[:5])} deg, not {a:g}: NX matches halves of "
            "unequal height (true_draft) or adds matching faces at the parting; see faces",
        )
    _grown(tx, body, d, reach)
    return {**out, "faces": drafted}


def _grown(tx, body, d, reach) -> None:
    """Warn when the body now reaches further along the draw direction: a parting draft
    keeps the far edges, so growth there means the faces were drafted out to the plane."""
    if reach is None:
        return
    now = _reach(tx, body, d)
    if now is None:
        return
    tol = 1e-4 * max(1.0, *(abs(v) for v in (*reach, *now)))
    if now[0] < reach[0] - tol or now[1] > reach[1] + tol:
        tx.warn(
            "NX_UPDATE_WARNING",
            f"The body now spans {now[0]:.6g}..{now[1]:.6g} along the draw direction (was "
            f"{reach[0]:.6g}..{reach[1]:.6g}): the draft grew it past its far edges; undo "
            "and move the parting plane into the faces",
        )

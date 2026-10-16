"""Synchronous relate-face edits: Make Coplanar, Coaxial, Parallel, Perpendicular, Tangent
and Symmetric (tool-surface 3.6), for adapting imported (dumb) solids.

NX 2406 lessons (probed live): every builder takes the moving face in a ``SelectObject``
(Coplanar: ``ConstraintFace``; the others: ``MotionFace``) and needs it in its motion-group
collector too (Coplanar: ``RigidFace``, the others: ``MoveAlongFace``, both
``FaceRecognitionBuilder.FaceCollector``), else the commit fails "Feature has zero
parameters". The stationary face may lie on another body and stays put; the moved face
keeps its tag. Parallel keeps the face centre by default but Perpendicular does not, so
both get ``ThroughPoint`` (the face centre unless given). Symmetric takes a new plane
(``SymmetryPlaneOption.New``). Make Offset (``MotionFace``, ``StationaryFace``,
``OffsetValue``) needs both faces on one body ("Feature cannot be applied to multiple
bodies") and a positive distance (a negative one fails "A face does not intersect properly").

Not offered: Make Fixed (``FixedBuilder.FaceToFix``) refuses "Invalid constraint" on NX 2406
for a plain planar face, with or without recognized relations.

Not offered: Shell Face, Shell Body (``AdaptiveShellBuilder``) and Change Shell Thickness
commit without any effect through NXOpen on NX 2406, or refuse "Invalid shell data", on
parametric and unparameterized solids alike; ``ShowResults`` on Shell Body crashed NX's
memory access. nx_create_shell (``details``) thin-walls a dumb solid instead.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, FaceRef, Items, Length, Plane, Point3
from nx_mcp.domains.modeling import common
from nx_mcp.kit import args as kargs
from nx_mcp.kit import builders, expr, geom, licenses, measure, readback, select

SYNC_LICENSE = "nx_synchronous"

Relation = Literal[
    "coplanar", "coaxial", "parallel", "perpendicular", "tangent", "symmetric", "offset"
]
AXIAL = (select.FACE_CYLINDER, select.FACE_CONE)
LINEAR = (select.FACE_PLANE, *AXIAL)  # faces with a direction: a normal or an axis
FACTORIES = {
    "coplanar": "CreateCoplanarBuilder",
    "coaxial": "CreateCoaxialBuilder",
    "parallel": "CreateParallelBuilder",
    "perpendicular": "CreatePerpendicularBuilder",
    "tangent": "CreateTangentBuilder",
    "symmetric": "CreateSymmetricBuilder",
    "offset": "CreateMakeOffsetBuilder",
}
# Face types each relation takes (both faces), said in its refusal.
TAKES = {
    "coplanar": ((select.FACE_PLANE,), "planar faces"),
    "offset": ((select.FACE_PLANE,), "planar faces"),
    "coaxial": (AXIAL, "cylindrical or conical faces"),
    "parallel": (LINEAR, "planar, cylindrical or conical faces"),
    "perpendicular": (LINEAR, "planar, cylindrical or conical faces"),
}
TOL = 1e-5  # relative: NX's modeling tolerance, not the floating-point one


def synchronous(tx, *, by_argument: bool = False):
    """The context of NX Synchronous work: NX's license refusal inside becomes
    ``NX_LICENSE_UNAVAILABLE`` naming ``nx_synchronous`` (``kit.licenses.attempt``)."""
    return licenses.attempt(tx, (SYNC_LICENSE,), by_argument=by_argument)


class _RelateData(common.FeatureData):
    volume_change: Annotated[float, Doc("Volume after minus before")]
    angle: Annotated[
        float | None, Doc("Degrees between the face's and to's normal or axis lines after, 0-90")
    ]
    gap: Annotated[float | None, Doc("Residual distance the relation reads back (0 when met)")]


class RelateData(_RelateData, total=False):
    distance: Annotated[float, Doc("offset: the distance reached between the two faces")]


def _shape(tx, face):
    """``{type, point, dir, radius}`` of a face from UF face data: a plane's point and
    normal, a cylinder's or cone's axis point and direction, a sphere's centre."""
    data = tx.uf.Modeling.AskFaceData(face.Tag)
    kind = int(data[0])
    direction = [float(v) for v in data[2]]
    return {
        "type": kind,
        "point": [float(v) for v in data[1]],
        "dir": geom.unit(direction) if kind in LINEAR else direction,
        "radius": float(data[4]),
    }


def _alive_shape(tx, face, before):
    """:func:`_shape` of a face, or ``before`` when NX deleted it (merged into another)."""
    try:
        return _shape(tx, face)
    except Exception:  # "Solid Object Input Are Not Alive" (875288): the face is gone
        return before


def _line_gap(point, origin, direction) -> float:
    return geom.norm(geom.cross(geom.sub(point, origin), direction))


def _angle(a, b) -> float:
    """Degrees between two direction lines, 0-90."""
    return math.degrees(math.acos(max(-1.0, min(1.0, abs(geom.dot(a, b))))))


def _tangent_gap(m, s) -> float | None:
    """How far a plane, cylinder or sphere pair is from tangency, None when unknown."""
    kinds = {m["type"], s["type"]}
    if m["type"] == select.FACE_PLANE and s["type"] == select.FACE_PLANE:
        return None
    if select.FACE_PLANE in kinds:
        plane, other = (m, s) if m["type"] == select.FACE_PLANE else (s, m)
        if other["type"] not in (select.FACE_CYLINDER, select.FACE_SPHERE):
            return None
        if (
            other["type"] == select.FACE_CYLINDER
            and abs(geom.dot(other["dir"], plane["dir"])) > TOL
        ):
            return None
        d = abs(geom.dot(geom.sub(other["point"], plane["point"]), plane["dir"]))
        return abs(d - other["radius"])
    if kinds <= {select.FACE_CYLINDER, select.FACE_SPHERE}:
        if m["type"] == s["type"] == select.FACE_SPHERE:
            d = geom.dist(m["point"], s["point"])
        elif m["type"] == s["type"]:
            if _angle(m["dir"], s["dir"]) > 1e-4:
                return None
            d = _line_gap(m["point"], s["point"], s["dir"])
        else:
            axis, ball = (m, s) if m["type"] == select.FACE_CYLINDER else (s, m)
            d = _line_gap(ball["point"], axis["point"], axis["dir"])
        rs = (m["radius"], s["radius"])
        return min(abs(d - sum(rs)), abs(d - abs(rs[0] - rs[1])))
    return None


def _check_faces(relation, m, s) -> None:
    if relation in TAKES:
        kinds, words = TAKES[relation]
        for shape, field in ((m, "/face"), (s, "/to")):
            if shape["type"] not in kinds:
                fail("NX_WRONG_GEOMETRY", f"{relation} takes {words}", field=field)
    elif relation == "symmetric" and m["type"] != s["type"]:
        fail("NX_WRONG_GEOMETRY", "symmetric takes two faces of the same type (both planar, "
             "both cylindrical...)", field="/to")  # fmt: skip
    elif relation == "tangent" and m["type"] == s["type"] == select.FACE_PLANE:
        fail("NX_WRONG_GEOMETRY", "Two planes are never tangent: use coplanar",
             field="/to")  # fmt: skip


def _through(tx, face, m):
    """The face's centre: its bounding-box centre projected onto its plane or axis."""
    low, high = measure.bounds(tx, face)
    centre = [(a + b) / 2 for a, b in zip(low, high, strict=True)]
    offset = geom.dot(geom.sub(centre, m["point"]), m["dir"])
    if m["type"] == select.FACE_PLANE:
        return geom.sub(centre, geom.scale(m["dir"], offset))
    return geom.add(m["point"], geom.scale(m["dir"], offset))


def _wanted_angle(relation, m, s) -> float | None:
    """Degrees the relation leaves between the two faces' normal or axis lines, None when
    it fixes none: tangent turns a cylinder's axis square to a plane's normal (90) and
    keeps two cylinders parallel (0); a cone's half angle leaves it open."""
    if relation == "perpendicular":
        return 90.0
    if relation != "tangent":
        return 0.0
    kinds = sorted((m["type"], s["type"]))
    if kinds == sorted((select.FACE_PLANE, select.FACE_CYLINDER)):
        return 90.0
    if kinds == [select.FACE_CYLINDER, select.FACE_CYLINDER]:
        return 0.0
    return None


def _read_back(relation, m, s, mirror, through, distance=0.0):
    """``(angle, gap)`` after the edit; a relation NX did not meet fails the call."""
    scale = max(1.0, *map(abs, m["point"]), *map(abs, s["point"]))
    tol = TOL * scale
    angle = _angle(m["dir"], s["dir"]) if {m["type"], s["type"]} <= set(LINEAR) else None
    gap = None
    if relation == "coplanar":
        gap = abs(geom.dot(geom.sub(m["point"], s["point"]), s["dir"]))
    elif relation == "offset":
        gap = abs(abs(geom.dot(geom.sub(m["point"], s["point"]), s["dir"])) - distance)
    elif relation == "coaxial":
        gap = _line_gap(m["point"], s["point"], s["dir"])
    elif relation == "tangent":
        gap = _tangent_gap(m, s)
    elif relation == "symmetric":
        origin, normal = mirror
        point = geom.reflect(s["point"], origin, normal)
        if s["type"] in LINEAR:
            image = geom.sub(s["dir"], geom.scale(normal, 2.0 * geom.dot(s["dir"], normal)))
            angle = _angle(m["dir"], image)
            if s["type"] == select.FACE_PLANE:
                gap = abs(geom.dot(geom.sub(m["point"], point), m["dir"]))
            else:
                gap = _line_gap(m["point"], point, image)
        else:
            gap = geom.dist(m["point"], point)
    if through is not None and m["type"] == select.FACE_PLANE:
        readback.expect_close(geom.dot(geom.sub(through, m["point"]), m["dir"]), 0.0,
                              "through", tol=tol)  # fmt: skip
    wanted = _wanted_angle(relation, m, s)
    if angle is not None and wanted is not None:
        readback.expect_close(angle, wanted, "angle", tol=1e-3)
    if gap is not None:
        readback.expect_close(gap, 0.0, "gap", tol=tol)
    return angle, gap


@tool(
    effect="model",
    label="Relate faces",
    summary="Make {face} {relation} to {to}",
    keywords=(
        "make coplanar",
        "make coaxial",
        "make parallel",
        "make perpendicular",
        "make tangent",
        "make symmetric",
        "make offset",
        "make holes concentric",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="relation",
    cost="heavy",
    previewable=True,
    example={"face": "fc:9", "relation": "coplanar", "to": "fc:14"},
)
def nx_edit_face_relation(
    tx: Tx,
    *,
    face: Annotated[FaceRef, Doc("The face that moves")],
    relation: Relation,
    to: Annotated[FaceRef, Doc("Stationary face (any body); symmetric: the face to mirror")],
    plane: Annotated[Plane, Doc("symmetric only (required): the symmetry plane")] | None = None,
    through: Annotated[Point3, Doc("parallel/perpendicular: point the turned face passes through")]
    | None = None,
    with_faces: Annotated[
        list[FaceRef], Items(1, 100), Doc("Faces of the same body that move rigidly along")
    ]
    | None = None,
    distance: Annotated[Length, Doc("offset only (required, > 0): gap from to's plane")]
    | None = None,
) -> RelateData:
    """Make a face coplanar, coaxial, parallel, perpendicular, tangent, symmetric or offset to
    another face. Synchronous: imported solids too; to stays put, adjacent faces stretch to
    follow. coplanar: planes (flush); coaxial: cylinders/cones (concentric holes); parallel and perpendicular turn the face
    about its centre (or through); symmetric: face becomes to's mirror image about plane;
    offset: parallel plane of the same body at distance. Reads back angle and gap."""
    if (plane is not None) != (relation == "symmetric"):
        verb = "needs" if relation == "symmetric" else "does not take"
        fail("NX_INVALID_ARGUMENT", f"{relation} {verb} plane", field="/plane",
             hint="symmetric takes plane: 'yz' or {origin, normal}, a datum plane or a face")  # fmt: skip
    if through is not None and relation not in ("parallel", "perpendicular"):
        fail("NX_INVALID_ARGUMENT", f"{relation} does not take through", field="/through",
             hint="through applies to parallel and perpendicular")  # fmt: skip
    if (distance is not None) != (relation == "offset"):
        verb = "needs" if relation == "offset" else "does not take"
        fail("NX_INVALID_ARGUMENT", f"{relation} {verb} distance", field="/distance",
             hint="offset takes distance > 0: the gap between the two parallel faces")  # fmt: skip
    if distance is not None and float(distance) <= 0:
        fail("NX_INVALID_ARGUMENT", "distance must be positive", field="/distance")
    if with_faces and relation == "offset":
        fail("NX_INVALID_ARGUMENT", "offset does not take with_faces", field="/with_faces")
    if face == to:
        fail("NX_INVALID_ARGUMENT", "face and to must differ", field="/to")
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        moving = select.one(tx, face, "face", field="/face")
        fixed = select.resolve(tx, to, "face", field="/to")
        if not select.owned_by_work_part(tx, fixed):
            fail("NX_WRONG_OWNER", "face and to must be in the same part", field="/to",
                 hint="Pick to among the faces of face's part (any body of it)")  # fmt: skip
        if int(moving.Tag) == int(fixed.Tag):
            fail("NX_INVALID_ARGUMENT", "face and to must differ", field="/to")
        body = moving.GetBody()
        group = select.faces(tx, with_faces or [], "/with_faces")
        for i, extra in enumerate(group):
            if int(extra.GetBody().Tag) != int(body.Tag):
                fail("NX_WRONG_OWNER", "with_faces must be faces of face's body",
                     field=f"/with_faces/{i}")  # fmt: skip
        group = [g for g in group if int(g.Tag) not in (int(moving.Tag), int(fixed.Tag))]
        if relation == "offset" and int(fixed.GetBody().Tag) != int(body.Tag):
            fail("NX_WRONG_OWNER", "offset needs to on face's body", field="/to")
        m, s = _shape(tx, moving), _shape(tx, fixed)
        _check_faces(relation, m, s)
        mirror = select.plane(tx, plane, "/plane")[1:] if plane is not None else None
        point = None
        if relation in ("parallel", "perpendicular"):
            point = (
                kargs.vec3(through, "/through") if through is not None else _through(tx, moving, m)
            )
    before = measure.volume(tx, body)
    part = tx.part
    rule = part.ScRuleFactory.CreateRuleFaceDumb([moving, *group])
    with (
        synchronous(tx),
        builders.build(tx, getattr(part.Features, FACTORIES[relation]), None) as b,
    ):
        if relation == "offset":
            b.MotionFace.Value = moving
            expr.drive(b.OffsetValue, distance)
        elif relation == "coplanar":
            b.ConstraintFace.Value = moving
            b.RigidFace.FaceCollector.ReplaceRules([rule], False)
        else:
            b.MotionFace.Value = moving
            b.MoveAlongFace.FaceCollector.ReplaceRules([rule], False)
        b.StationaryFace.Value = fixed
        if point is not None:
            b.ThroughPoint = builders.point(tx, point)
        if mirror is not None:
            b.SymmetryPlaneOption = tx.nx.Features.SymmetricBuilder.PlaneOptions.New
            b.NewSymmetryPlane = part.Planes.CreatePlane(
                tx.nx.Point3d(*mirror[0]),
                tx.nx.Vector3d(*mirror[1]),
                tx.nx.SmartObject.UpdateOption.WithinModeling,
            )
        feature = b.commit()
    check = getattr(tx.uf.Modeling, "AskBodyConsistency", None)
    if check is not None:
        faults = check(body.Tag)[0]
        readback.expect(not faults, "consistency", "The edit left an invalid body", faults=faults)
    # The stationary face stays put; NX may merge it away (coplanar to a face of the same
    # body keeps the moved face and deletes the other), so a dead one reads as before.
    still = _alive_shape(tx, fixed, s)
    moved = _alive_shape(tx, moving, None)
    if moved is None:  # merged into the stationary face: only coplanar does that
        readback.expect(relation == "coplanar", "face", "NX deleted the moved face")
        moved = still
    after = moved, still
    angle, gap = _read_back(relation, *after, mirror, point, float(distance or 0.0))
    out = common.result_of(tx, feature, [body])
    return {
        **out,
        "volume_change": out["volume"] - before,
        "angle": angle,
        "gap": gap,
        **_reached(tx, relation, moving, s),
    }


def _reached(tx, relation, moving, s) -> dict:
    """offset: the distance reached between the moved face and to's plane (to stays put)."""
    if relation != "offset":
        return {}
    here = _shape(tx, moving)
    return {"distance": abs(geom.dot(geom.sub(here["point"], s["point"]), s["dir"]))}

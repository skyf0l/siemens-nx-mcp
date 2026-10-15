"""Offset Surface: sheets at a distance from faces (laminate thickness, skin offsets).

NX 2406 lessons kept here:

- ``CreateOffsetSurfaceBuilder`` needs the basic freeform license (``nx_freeform_1``),
  checked with ``LicenseManager.CheckPresence`` before the builder is created.
- ``Tolerance`` defaults to 0 and is always set (0.001 mm in part units by default).
- Each face set (``Part.FaceSetOffsets.CreateEmptyFaceSet``) is a face collector and
  an ``Offset`` expression; ``FaceSets.Append`` adds it. The distance is signed: a
  negative expression offsets against the face normal (UF ``AskFaceProps`` normals,
  outward on a solid): a cylinder face r 20 offset -5 gives r 15. An analytic face
  stays analytic (cylinder r 25 for +5).
- A face in two sets is silently offset once (the first set): refused before NX, so
  the two sides of a face are two calls. A zero distance is NX's 821043 ("Supplied
  tolerance less than Parasolids"): refused before NX.
- One feature is built for all face sets, but faces that are not connected give one
  sheet body each and ``Feature.GetBodies()`` lists only the first: the result bodies
  are the part's new bodies (whatever ``OutputOption``; ``GetCommittedObjects`` is
  empty).
- A native refusal (an offset larger than a concave radius) is ``NX_BUILD_FAILED``
  with ``nx_code``.

Read back: at up to 3 x 3 interior points of every source face, the point moved by
the signed distance along the face normal lies on the result (``gap``), and the
nearest result point is the distance away (``measured_min``/``measured_max``).
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, FaceRef, Items, Length, forbid_extra
from nx_mcp.domains.freeform import common
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import builders, curve, expr, facts, geom, readback, result, select, units

MAX_FACES = 500
SAMPLES = 3  # per face and UV direction


class OffsetSet(TypedDict):
    faces: Annotated[list[FaceRef], Items(1, 200)]
    distance: Annotated[Length, Doc("Signed: + along the face normal, - against it")]


forbid_extra(OffsetSet)


class SetData(TypedDict):
    distance: float
    faces: int
    samples: int  # interior points checked
    measured_min: float  # nearest result distance over the samples
    measured_max: float
    gap: float  # largest distance of point + distance * normal from the result


class OffsetData(common.SheetData):
    sets: list[SetData]
    skipped: list[result.Row]  # partial: faces NX could not offset (left out)


def _too_far(tx, resolved) -> None:
    """Refuse an offset towards the axis or centre of a cylinder or sphere face by its radius
    or more (NX: "could not be offset"), naming the face and the largest inward distance."""
    for i, (found, value) in enumerate(resolved):
        for j, face in enumerate(found):
            kind, centre, axis = select.face_data(tx, face)
            if kind not in (select.FACE_CYLINDER, select.FACE_SPHERE):
                continue
            samples = facts.interior_samples(tx, face, 1)
            if not samples:
                continue
            point, normal = samples[0]
            radial = geom.sub(point, centre)
            if kind == select.FACE_CYLINDER:
                along = geom.unit(axis)
                radial = geom.sub(radial, geom.scale(along, geom.dot(radial, along)))
            radius = geom.norm(radial)
            inward = geom.dot(normal, radial) * float(value) < 0
            if inward and abs(float(value)) >= radius - units.part_tolerance(tx):
                where = f"/sets/{i}" if len(resolved) > 1 else ""
                fail("NX_INVALID_ARGUMENT", f"Face {j} of radius {radius:.4g} cannot be offset "
                     f"{abs(float(value)):.4g} towards its centre", field=f"{where}/distance",
                     hint=f"Offset less than {radius:.4g} inward, or pass partial true to leave "
                     "such faces out", details={"face": tx.ref(face, kind="face"),
                                                "radius": radius})  # fmt: skip


def _sets(faces, distance, sets):
    """``[(face refs, distance, field)]`` from faces + distance or from sets."""
    if sets is not None:
        if faces is not None or distance is not None:
            fail("NX_INVALID_ARGUMENT", "Send faces and distance, or sets, not both",
                 field="/sets")  # fmt: skip
        out = [(list(s["faces"]), s["distance"], f"/sets/{i}") for i, s in enumerate(sets)]
    else:
        if faces is None or distance is None:
            fail(
                "NX_INVALID_ARGUMENT",
                "Send faces and distance (or sets)",
                field="/faces" if faces is None else "/distance",
                hint="faces: ['fc:7'], distance: 2 offsets 2 along the face normal; -2 against",
            )
        out = [(list(faces), distance, "")]
    seen: set[str] = set()
    for refs, value, where in out:
        if geom.close(float(value), 0.0, 1e-12):
            fail("NX_INVALID_ARGUMENT", "The offset distance must not be zero",
                 field=f"{where}/distance")  # fmt: skip
        for j, ref in enumerate(refs):
            if ref in seen:
                fail("NX_INVALID_ARGUMENT", f"Face {ref} is listed twice",
                     field=f"{where}/faces/{j}")  # fmt: skip
            seen.add(ref)
    if len(seen) > MAX_FACES:
        fail("NX_TOO_MANY_OBJECTS", f"At most {MAX_FACES} faces", field="/sets")
    return out


def _check(tx, faces, distance, bodies, limit) -> SetData:
    """Sampled offset of one face set: each interior sample moved by ``distance`` along
    its normal lies on the result; the nearest result point is ``|distance|`` away."""
    nearest, gaps = [], []
    for face in faces:
        tx.checkpoint()
        for point, normal in facts.interior_samples(tx, face, SAMPLES):
            target = geom.add(point, geom.scale(normal, distance))
            gaps.append(min(curve.point_distance(tx, b, target)[0] for b in bodies))
            nearest.append(min(curve.point_distance(tx, b, point)[0] for b in bodies))
    readback.expect(bool(nearest), "samples", "No interior point found on the faces")
    out: SetData = {
        "distance": float(distance),
        "faces": len(faces),
        "samples": len(nearest),
        "measured_min": min(nearest),
        "measured_max": max(nearest),
        "gap": max(gaps),
    }
    readback.expect(out["gap"] <= limit, "offset", "The offset sheet is not at the distance "
                    "along the face normals", **out)  # fmt: skip
    spread = max(abs(m - abs(distance)) for m in (out["measured_min"], out["measured_max"]))
    readback.expect(spread <= limit, "distance", "The offset sheet is not at the distance",
                    **out)  # fmt: skip
    return out


@tool(
    effect="model",
    label="Create offset surface",
    summary=lambda a, say: (
        f"Offset {say('faces')} by {say('distance')}"
        if a.get("sets") is None
        else f"Offset {len(a['sets'])} face sets"
    ),
    keywords=(
        "offset surface",
        "offset face",
        "laminate thickness",
        "skin offset",
        "parallel surface",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:7"], "distance": "-5 mm"},
)
def nx_create_offset_surface(
    tx: Tx,
    *,
    faces: Annotated[list[FaceRef], Items(1, 200)] | None = None,
    distance: Annotated[Length, Doc("Signed: + along the face normal, - against it")] | None = None,
    sets: Annotated[list[OffsetSet], Items(1, 20), Doc("Face sets, each its own distance")]
    | None = None,
    tolerance: Annotated[Length, Doc("Offset tolerance")] | None = None,
    partial: Annotated[bool, Doc("Leave out faces NX cannot offset (listed in skipped)")] = False,
) -> OffsetData:
    """Offset sheet or solid faces into new sheets at a signed distance along the normals.
    sets give face groups their own distance. Checked at sampled points: sets[].gap and
    measured_min/max."""
    groups = _sets(faces, distance, sets)
    with tx.checks():
        tol = common.given_tolerance(tx, tolerance)
        resolved = [
            (select.faces(tx, refs, f"{where}/faces"), value) for refs, value, where in groups
        ]
        tags = [int(f.Tag) for found, _ in resolved for f in found]
        if len(set(tags)) != len(tags):
            fail("NX_INVALID_ARGUMENT", "A face is listed twice", field="/sets")
        if not partial:
            _too_far(tx, resolved)
    common.licensed(tx)
    part = tx.part
    before = {int(x.Tag) for x in part.Bodies}
    with builders.build(tx, part.Features.CreateOffsetSurfaceBuilder, None) as b:
        b.Tolerance = tol
        b.OutputOption = tx.nx.Features.OffsetSurfaceBuilder.OutputOptionType.OneFeatureForAllFaces
        for found, value in resolved:
            face_set = part.FaceSetOffsets.CreateEmptyFaceSet()
            face_set.Offset.RightHandSide = expr.rhs(value)
            face_set.FaceCollector = builders.collector(tx, found, "Face")
            b.FaceSets.Append(face_set)
        if partial:
            b.PartialOption = True
        feature = b.commit()
    bodies = common.feature_bodies(feature)
    known = {int(x.Tag) for x in bodies}
    bodies += [x for x in part.Bodies if int(x.Tag) not in before | known]
    out = common.result_of(tx, feature, bodies)
    kbodies.expect_sound(tx, bodies)
    limit = 2 * tol + 10 * units.part_tolerance(tx)
    skipped: list = []
    if partial:
        resolved = [(_offset_faces(tx, found, float(v), bodies, limit, skipped), v)
                    for found, v in resolved]  # fmt: skip
        if skipped:
            tx.warn("NX_APPROXIMATED", f"{len(skipped)} face(s) could not be offset and were "
                    "left out (skipped)")  # fmt: skip
    checked = [_check(tx, found, float(value), bodies, limit) for found, value in resolved if found]
    return {**out, "sets": checked, "skipped": [tx.ref(f, kind="face") for f in skipped]}


def _offset_faces(tx, faces, distance, bodies, limit, skipped) -> list:
    """The faces of a partial offset that NX offset; the others go to ``skipped``."""
    kept = []
    for face in faces:
        try:
            _check(tx, [face], distance, bodies, limit)
        except NXError as error:
            if error.code != "NX_VERIFICATION_FAILED":
                raise
            skipped.append(face)
            continue
        kept.append(face)
    readback.expect(bool(kept) or not faces, "offset", "NX offset none of the faces")
    return kept

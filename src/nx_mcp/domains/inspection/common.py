"""Helpers shared by the inspection tools (this domain only).

- ``targets``: bodies named by references (bodies, features, components) or a scope.
- ``face_samples``: a trimmed UV grid on faces (UF ``AskFaceProps``), shared by the
  face analysis and the wall thickness.
- ``radii``: NX 2406 reports a straight principal direction as a huge radius
  (1e10 inch = 3.937e8 mm; about 1e30 on later builds): any radius past ``FLAT_RADIUS``
  part units is straight (radius None, curvature 0).
"""

from __future__ import annotations

import math

from nx_mcp.core.errors import fail, native
from nx_mcp.kit import bodies, explode, facts, geom

# Principal radii at or beyond this many part units mean a straight (flat) direction.
FLAT_RADIUS = 1e8
# UF_MODL_ask_point_containment: 1 inside, 2 outside, 3 on the boundary.
INSIDE, OUTSIDE = 1, 2
MAX_SAMPLES = 10000
TARGET_KINDS = ("body", "feature", "component")


def target_bodies(tx, refs, scope="auto", field="/bodies"):
    """Bodies of ``refs`` (bodies, features, components with their children), or of
    ``scope`` when ``refs`` is None; never empty."""
    if refs is None:
        return bodies.require(bodies.scope(tx, scope), field=field)
    found, seen = [], set()
    for i, ref in enumerate(refs):
        obj = tx.resolve(ref, *TARGET_KINDS)
        for body in bodies.of(tx, obj):
            if int(body.Tag) not in seen:
                seen.add(int(body.Tag))
                found.append(body)
        if not found:
            bodies.require(found, field=f"{field}/{i}")
    return bodies.require(found, field=field)


def note_exploded(tx) -> None:
    """``NX_NOTE`` once per call while an explosion is shown: NX measures assembled poses."""
    note = explode.assembled_note(tx)
    if note is not None and all(w.get("message") != note for w in tx.warnings):
        tx.warn("NX_NOTE", note)


def kind_of(tx, obj):
    """The reference kind of a resolved object; curves and points are recognised by their
    members where the host cannot classify them (a sketch ``Line`` on NX 2406)."""
    if hasattr(obj, "SolidEdgeType"):
        return "edge"
    if hasattr(obj, "StartPoint") or hasattr(obj, "CenterPoint") or hasattr(obj, "Get3DPoles"):
        return "curve"
    try:
        return tx.kind_of(obj)
    except Exception:
        if hasattr(obj, "Coordinates"):
            return "point"
        raise


def count_text(args, field, noun):
    n = len(args.get(field) or [])
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def radii(values):
    """``(radii, curvatures)``: straight directions are radius None, curvature 0."""
    out_r, out_k = [], []
    for r in values:
        r = float(r)
        if abs(r) >= FLAT_RADIUS:
            out_r.append(None)
            out_k.append(0.0)
        elif abs(r) > 1e-12:
            out_r.append(r)
            out_k.append(1.0 / r)
        else:
            out_r.append(None)
            out_k.append(None)
    return out_r, out_k


def face_samples(tx, faces, per_axis, field="/samples_per_axis"):
    """``([(face, uv, props)], skipped)`` on a ``per_axis`` x ``per_axis`` UV grid of each
    face; points outside the trimmed face are skipped. ``props`` is UF
    ``AskFaceProps``: point, du, dv, d2u, d2v, unit normal, principal radii."""
    if len(faces) * per_axis**2 > MAX_SAMPLES:
        fail(
            "NX_TOO_MANY_OBJECTS",
            f"{len(faces) * per_axis**2} samples; at most {MAX_SAMPLES} per call",
            field=field,
            hint="Select fewer faces or lower samples_per_axis",
        )
    modeling = tx.uf.Modeling
    samples, skipped = [], 0
    for face in faces:
        tx.checkpoint()
        with native():
            u0, u1, v0, v1 = (float(v) for v in modeling.AskFaceUvMinmax(face.Tag))
            for i in range(per_axis):
                for j in range(per_axis):
                    uv = [
                        u0 + (u1 - u0) * (i + 0.5) / per_axis,
                        v0 + (v1 - v0) * (j + 0.5) / per_axis,
                    ]
                    props = modeling.AskFaceProps(face.Tag, uv)
                    point = [float(v) for v in props[0]]
                    if int(modeling.AskPointContainment(point, face.Tag)) == OUTSIDE:
                        skipped += 1
                        continue
                    samples.append((face, uv, props))
    return samples, skipped


def note_unsampled(tx, faces, samples, field="samples_per_axis") -> None:
    """``NX_NOTE`` naming the faces where every grid point fell outside the trim (a
    hole or slot under the points): their rows are empty, not a measurement."""
    hit = {int(face.Tag) for face, _uv, _props in samples}
    empty = [tx.ref(f, kind="face")["id"] for f in faces if int(f.Tag) not in hit]
    if not empty:
        return
    which = "every face" if len(empty) == len(faces) else f"{len(empty)} of {len(faces)} faces"
    shown = ", ".join(empty[:10]) + (", ..." if len(empty) > 10 else "")
    tx.warn(
        "NX_NOTE",
        f"No sample point lies on {which} ({shown}): each grid point fell outside the "
        f"trimmed face (a hole or slot there); raise {field} for samples on them",
    )


def xyz3(values):
    return [float(v) for v in list(values)[:3]]


def unit_or_none(values):
    v = xyz3(values)
    n = geom.norm(v)
    return [x / n for x in v] if n > 1e-12 else None


def axial_facts(tx, face, data) -> dict:
    """Facts of a cylinder (code 16) or cone (17) face from UF ``AskFaceData`` ``data``.

    Cylinders: ``radius``, ``axis``, ``axis_point``. Cones: ``axis``, ``axis_point``,
    ``half_angle`` (degrees) and ``radii``, the radii of the face's circular edges in
    order along ``axis`` (both ends of a frustum; one for a cone with an apex). NX 2406:
    a cone's ``data[4]`` is its radius at ``axis_point`` and ``data[5]`` the half-angle
    in radians; UF ``AskArcData`` centres are arc-local, so edges go through UF Eval.
    """
    code = int(data[0])
    point, axis = xyz3(data[1]), geom.unit(xyz3(data[2]))
    if code == 16:
        return {"radius": float(data[4]), "axis": axis, "axis_point": point}
    rings: dict[float, float] = {}  # axial position -> radius (split arcs counted once)
    for edge in face.GetEdges():
        found = facts.edge(tx, edge)
        if found["kind"] == "arc":
            at = geom.dot(geom.sub(found["center"], point), axis)
            rings.setdefault(round(at, 6), float(found["radius"]))
    return {
        "axis": axis,
        "axis_point": point,
        "half_angle": math.degrees(abs(float(data[5]))),
        "radii": [rings[k] for k in sorted(rings)],
    }

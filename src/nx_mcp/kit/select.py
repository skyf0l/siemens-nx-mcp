"""Resolve references to work-part geometry and the Plane/Axis/Direction unions.

Every resolver goes through ``tx.resolve(ref, *kinds)`` (stale, wrong-kind and
wrong-owner refs fail there) and then checks that the object is owned by the
work part, not an assembly occurrence. Face geometry is read with UF
``Modeling.AskFaceData``; on NX 2406 its direction is the outward face normal
for planes and the axis for cylinders and cones.
"""

from __future__ import annotations

from nx_mcp.core.errors import fail
from nx_mcp.kit import geom

# UF_MODL face types (AskFaceData[0]).
FACE_CYLINDER = 16
FACE_CONE = 17
FACE_SPHERE = 18
FACE_PLANE = 22

PRINCIPAL_AXES = {
    "x": [1.0, 0.0, 0.0],
    "y": [0.0, 1.0, 0.0],
    "z": [0.0, 0.0, 1.0],
    "-x": [-1.0, 0.0, 0.0],
    "-y": [0.0, -1.0, 0.0],
    "-z": [0.0, 0.0, -1.0],
}
PRINCIPAL_PLANES = {"xy": [0.0, 0.0, 1.0], "yz": [1.0, 0.0, 0.0], "xz": [0.0, -1.0, 0.0]}
ORIGIN = [0.0, 0.0, 0.0]


def owned_by_work_part(tx, obj):
    """True for a prototype object of the work part (not an occurrence)."""
    if getattr(obj, "IsOccurrence", False):
        return False
    return getattr(obj, "OwningPart", None) == tx.part


def owned(tx, obj, field=None, what="geometry"):
    """``obj`` when it is owned by the work part, else ``wrong_owner``."""
    if not owned_by_work_part(tx, obj):
        fail("NX_WRONG_OWNER", f"Select {what} owned by the work part", field=field)
    return obj


def one(tx, ref, *kinds, field=None):
    """Resolve one reference of ``kinds`` owned by the work part."""
    return owned(
        tx, tx.resolve(ref, *kinds), field, kinds[0].replace("_", " ") if kinds else "geometry"
    )


def _many(tx, refs, kind, field):
    seen, result = set(), []
    for i, ref in enumerate(refs):
        obj = one(tx, ref, kind, field=f"{field}/{i}" if field else None)
        tag = int(obj.Tag)
        if tag in seen:
            fail("NX_INVALID_ARGUMENT", f"{kind} {ref} is listed twice", field=field)
        seen.add(tag)
        result.append(obj)
    return result


def bodies(tx, refs, field="/bodies"):
    return _many(tx, refs, "body", field)


def faces(tx, refs, field="/faces"):
    return _many(tx, refs, "face", field)


def edges(tx, refs, field="/edges"):
    return _many(tx, refs, "edge", field)


def face_data(tx, face):
    """``(type, point, direction)`` of a face from UF AskFaceData."""
    data = tx.uf.Modeling.AskFaceData(face.Tag)
    return int(data[0]), [float(v) for v in data[1]], [float(v) for v in data[2]]


def _is_datum_plane(obj):
    return hasattr(obj, "Normal") and hasattr(obj, "Origin")


def _is_datum_axis(obj):
    return hasattr(obj, "Direction") and hasattr(obj, "Origin")


def _is_edge(obj):
    return hasattr(obj, "SolidEdgeType")


def planar(tx, ref, field="/reference"):
    """``(obj, origin, unit normal)`` of a planar face or datum plane of the work part."""
    obj = one(tx, ref, "datum_plane", "face", field=field)
    return (obj, *_plane_of(tx, obj, field))


def _plane_of(tx, obj, field):
    if _is_datum_plane(obj):
        return geom.xyz(obj.Origin), geom.unit(geom.xyz(obj.Normal), field)
    if hasattr(obj, "Origin"):
        # Datum axes and CSYS are datums too; AskFaceData on them raises a raw
        # NX error (820022) instead of a stable code.
        fail("NX_WRONG_KIND", "Select a planar face or a datum plane", field=field)
    kind, point, normal = face_data(tx, obj)
    if kind != FACE_PLANE:
        fail("NX_WRONG_GEOMETRY", "Select a planar face or a datum plane", field=field)
    return point, geom.unit(normal, field)


def plane(tx, value, field="/plane"):
    """``(obj or None, origin, unit normal)`` of the Plane union.

    ``value`` is a ref (datum plane or planar face), ``{"principal": "xy"}``
    or ``{"origin": Point3, "normal": Vec3}``.
    """
    if isinstance(value, str):
        return planar(tx, value, field)
    if isinstance(value, dict) and "principal" in value:
        key = value["principal"]
        if key not in PRINCIPAL_PLANES:
            fail(
                "NX_INVALID_ARGUMENT", "principal must be xy, yz or xz", field=field + "/principal"
            )
        return None, list(ORIGIN), list(PRINCIPAL_PLANES[key])
    if isinstance(value, dict) and {"origin", "normal"} <= set(value):
        from nx_mcp.kit.args import vec3

        origin = vec3(value["origin"], field + "/origin")
        return None, origin, geom.unit(value["normal"], field + "/normal")
    fail(
        "NX_INVALID_ARGUMENT", "plane must be a reference, principal or origin+normal", field=field
    )


def _edge_line(tx, edge, field):
    if edge.SolidEdgeType != tx.nx.Edge.EdgeType.Linear:
        fail("NX_WRONG_GEOMETRY", "Select a linear edge", field=field)
    start, end = (geom.xyz(p) for p in edge.GetVertices())
    if geom.dist(start, end) <= geom.tol(start, end):
        fail("NX_WRONG_GEOMETRY", "The edge has zero length", field=field)
    return start, geom.unit(geom.sub(end, start), field)


def axis(tx, value, field="/axis"):
    """``(obj or None, origin, unit direction)`` of the Axis union.

    ``value`` is a ref (datum axis, linear edge, cylindrical or conical face),
    ``{"principal": "x"..."-z"}`` or ``{"origin": Point3, "direction": Vec3}``.
    """
    if isinstance(value, str):
        obj = one(tx, value, "datum_axis", "edge", "face", field=field)
        if _is_datum_axis(obj):
            return obj, geom.xyz(obj.Origin), geom.unit(geom.xyz(obj.Direction), field)
        if _is_edge(obj):
            return (obj, *_edge_line(tx, obj, field))
        kind, point, direction = face_data(tx, obj)
        if kind not in (FACE_CYLINDER, FACE_CONE):
            fail("NX_WRONG_GEOMETRY", "Select a cylindrical or conical face", field=field)
        return obj, point, geom.unit(direction, field)
    if isinstance(value, dict) and "principal" in value:
        key = value["principal"]
        if key not in PRINCIPAL_AXES:
            fail("NX_INVALID_ARGUMENT", "principal must be x, y, z, -x, -y or -z", field=field)
        return None, list(ORIGIN), list(PRINCIPAL_AXES[key])
    if isinstance(value, dict) and {"origin", "direction"} <= set(value):
        from nx_mcp.kit.args import vec3

        origin = vec3(value["origin"], field + "/origin")
        return None, origin, geom.unit(value["direction"], field + "/direction")
    fail(
        "NX_INVALID_ARGUMENT",
        "axis must be a reference, principal or origin+direction",
        field=field,
    )


def direction(tx, value, field="/direction"):
    """Unit vector of the Direction union: Vec3, ref or ``{"principal": ...}``.

    A ref is a linear edge (start to end), a datum axis or a planar face /
    datum plane (its normal).
    """
    if isinstance(value, (list, tuple)):
        return geom.unit(value, field)
    if isinstance(value, dict) and "principal" in value:
        return axis(tx, value, field)[2]
    if isinstance(value, str):
        obj = one(tx, value, "edge", "datum_axis", "datum_plane", "face", field=field)
        if _is_edge(obj):
            return _edge_line(tx, obj, field)[1]
        if _is_datum_axis(obj) and not _is_datum_plane(obj):
            return geom.unit(geom.xyz(obj.Direction), field)
        return _plane_of(tx, obj, field)[1]
    fail("NX_INVALID_ARGUMENT", "direction must be a vector, reference or principal", field=field)

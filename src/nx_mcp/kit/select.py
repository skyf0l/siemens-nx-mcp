"""Resolve references to work-part geometry and the Plane/Axis/Direction unions.

Every resolver goes through ``tx.resolve(ref, *kinds)`` (stale, wrong-kind and
wrong-owner refs fail there) and then checks that the object is owned by the
work part, not an assembly occurrence. Face geometry is read with UF
``Modeling.AskFaceData``; on NX 2406 its direction is the outward face normal
for planes and the axis for cylinders and cones.
"""

from __future__ import annotations

import re

from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.types import REF_PATTERN
from nx_mcp.core.validate import match_option
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
# NX view names of the principal planes (as core.types.PrincipalPlane).
PLANE_ALIASES = {"front": "xz", "top": "xy", "right": "yz"}
ORIGIN = [0.0, 0.0, 0.0]
AXIS_FORMS = "a ref, 'x'/'y'/'z'/'-x'/'-y'/'-z', {principal} or {origin, direction}"
PLANE_FORMS = "a ref, 'xy'/'yz'/'xz' (or 'XY plane', 'top'), {principal} or {origin, normal}"


def principal(value, options, aliases=None):
    """The principal option a bare or ``{"principal": ...}`` value names in any natural
    spelling ('-Z', '+y', 'z axis', 'XY plane', 'top'), else None; a ref-shaped string
    ('e:3') is never one."""
    if isinstance(value, dict):
        value = value.get("principal")
    if not isinstance(value, str) or re.match(REF_PATTERN, value):
        return None
    if value in options:
        return value
    return match_option(value, tuple(options), aliases or {})


def resolve(tx, ref, *kinds, field=None, work_part=False):
    """``tx.resolve`` whose stale, wrong-kind and wrong-owner errors name ``field``."""
    try:
        return (
            tx.resolve(ref, *kinds, work_part=work_part) if work_part else tx.resolve(ref, *kinds)
        )
    except NXError as error:
        if field and getattr(error, "field", None) is None:
            error.field = field
        raise


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
        tx,
        resolve(tx, ref, *kinds, field=field),
        field,
        kinds[0].replace("_", " ") if kinds else "geometry",
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


def curves(tx, refs, field="/curves"):
    return _many(tx, refs, "curve", field)


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


def planar(tx, ref, field="/reference", occurrences=False):
    """``(obj, origin, unit normal)`` of a planar face or datum plane of the work part;
    ``occurrences`` also takes one of a component in the work part's assembly (its
    origin and normal are then in the assembly's coordinates)."""
    if occurrences:
        obj = resolve(tx, ref, "datum_plane", "face", field=field)
        if not getattr(obj, "IsOccurrence", False):
            owned(tx, obj, field, "datum plane")
    else:
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


def plane(tx, value, field="/plane", occurrences=False):
    """``(obj or None, origin, unit normal)`` of the Plane union.

    ``value`` is a ref (datum plane or planar face), ``{"principal": "xy"}`` or a bare
    ``"xy"`` (``"XY plane"``, ``"top"``), or ``{"origin": Point3, "normal": Vec3}``;
    ``occurrences`` as in ``planar``.
    """
    key = principal(value, PRINCIPAL_PLANES, PLANE_ALIASES)
    if key is not None:
        return None, list(ORIGIN), list(PRINCIPAL_PLANES[key])
    if isinstance(value, str):
        return planar(tx, value, field, occurrences)
    if isinstance(value, dict) and "principal" in value:
        fail(
            "NX_INVALID_ARGUMENT",
            f"principal must be xy, yz or xz; a plane is {PLANE_FORMS}",
            field=field + "/principal",
        )
    if isinstance(value, dict) and {"origin", "normal"} <= set(value):
        from nx_mcp.kit.args import vec3

        origin = vec3(value["origin"], field + "/origin")
        return None, origin, geom.unit(value["normal"], field + "/normal")
    fail("NX_INVALID_ARGUMENT", f"a plane is {PLANE_FORMS}", field=field)


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
    ``{"principal": "x"..."-z"}`` or a bare ``"x"``..``"-z"`` (``"+Z"``, ``"z axis"``), or
    ``{"origin": Point3, "direction": Vec3}``.
    """
    key = principal(value, PRINCIPAL_AXES)
    if key is not None:
        return None, list(ORIGIN), list(PRINCIPAL_AXES[key])
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
        fail(
            "NX_INVALID_ARGUMENT",
            f"principal must be x, y, z, -x, -y or -z; an axis is {AXIS_FORMS}",
            field=field,
        )
    if isinstance(value, dict) and {"origin", "direction"} <= set(value):
        from nx_mcp.kit.args import vec3

        origin = vec3(value["origin"], field + "/origin")
        return None, origin, geom.unit(value["direction"], field + "/direction")
    fail("NX_INVALID_ARGUMENT", f"an axis is {AXIS_FORMS}", field=field)


def direction(tx, value, field="/direction"):
    """Unit vector of the Direction union: Vec3, ref, ``{"principal": ...}`` or a bare
    principal name (``"-z"``, ``"+Y"``, ``"x axis"``).

    A ref is a linear edge (start to end), a datum axis or a planar face /
    datum plane (its normal).
    """
    if isinstance(value, (list, tuple)):
        return geom.unit(value, field)
    if (isinstance(value, dict) and "principal" in value) or principal(value, PRINCIPAL_AXES):
        return axis(tx, value, field)[2]
    if isinstance(value, str):
        obj = one(tx, value, "edge", "datum_axis", "datum_plane", "face", field=field)
        if _is_edge(obj):
            return _edge_line(tx, obj, field)[1]
        if _is_datum_axis(obj) and not _is_datum_plane(obj):
            return geom.unit(geom.xyz(obj.Direction), field)
        return _plane_of(tx, obj, field)[1]
    fail(
        "NX_INVALID_ARGUMENT",
        "a direction is [x, y, z], a ref, 'x'/'y'/'z'/'-x'/'-y'/'-z' or {principal}",
        field=field,
    )

"""Legacy names for ``nx_mcp.kit`` helpers, used by the legacy executor and mixins.

Kit helpers raise ``core.errors.NXError``, which subclasses the legacy
``NXToolError``: legacy ``except NXToolError`` blocks catch them unchanged and the
legacy executor reports their code (not ``NX_API_ERROR``). Only the legacy
spellings remain here. Deleted with the legacy executor (F4).
"""

from __future__ import annotations

from nx_mcp.kit import args, geom

choice = args.choice
finite = args.finite
number = args.number
vector = args.vec3
point2 = args.point2
sheet_point = args.sheet_point
points3 = args.points3
object_id = args.ref
object_ids = args.refs
plane_through = geom.plane_through
orthonormal_frame = geom.orthonormal_frame
frame_from_normal = geom.frame_from_normal


def unit_normal(value, argument="normal"):
    """Unit 3-vector; malformed or zero input is NX_INVALID_ARGUMENT."""
    return geom.unit(value, argument)

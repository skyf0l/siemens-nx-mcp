"""Typed schemas: Body operations: asymmetric chamfer, variable-radius blend, mirror about a datum or face, split/trim/scale body.

Each public function is one MCP tool: its signature is the input schema and its
docstring the description. Handlers are BodyOpsMixin methods named "_" + name[3:].
READ_ONLY lists tools that never change NX state; NON_MODEL tools change NX
state but not model geometry (views, files). SUMMARIES maps a tool name to a
request_summary template (params, context) -> one line for the NX MCP panel.
Lengths are work-part units, angles degrees, points [x, y, z] in the work part.
"""

from __future__ import annotations

from typing import Literal

READ_ONLY: set[str] = set()
NON_MODEL: set[str] = set()


def _chamfer_summary(p, c):
    method = p.get("method", "symmetric")
    size = c.length(p.get("offset1"))
    if method == "two_offsets":
        size += f" x {c.length(p.get('offset2'))}"
    elif method == "offset_angle":
        size += f" at {p.get('angle')} deg"
    return f"Chamfer {c.count(p.get('edges'), 'edge')} ({method.replace('_', ' ')}, {size})"


def _blend_summary(p, c):
    n = len(p.get("points") or [])
    return f"Variable blend on {c.obj(p.get('edge'))} ({n} radius points)"


SUMMARIES: dict = {
    "nx_chamfer_advanced": _chamfer_summary,
    "nx_variable_blend": _blend_summary,
    "nx_mirror_body_about": lambda p, c: (
        f"Mirror {c.obj(p.get('body'))} about {c.obj(p.get('plane'))}"
    ),
    "nx_split_body": lambda p, c: f"Split {c.obj(p.get('body'))} by {c.obj(p.get('tool'))}",
    "nx_trim_body": lambda p, c: f"Trim {c.obj(p.get('body'))} by {c.obj(p.get('tool'))}",
    "nx_scale_body": lambda p, c: f"Scale {c.count(p.get('bodies'), 'body')} by {p.get('factor')}",
}


def nx_chamfer_advanced(
    edges: list[str],
    method: Literal["symmetric", "two_offsets", "offset_angle"],
    offset1: float,
    offset2: float | None = None,
    angle: float | None = None,
    reverse: bool = False,
):
    """Create a native chamfer feature on edge IDs of one owned solid body (lengths in part units). symmetric: both adjacent faces cut back by offset1 (offset2/angle not allowed). two_offsets: offset1 on NX's first adjacent face and offset2 on the other (both > 0). offset_angle: offset1 on the first face and angle in degrees (0 < angle < 90), so the other face is cut back by offset1*tan(angle). NX picks the first face from the edge's orientation; reverse=true swaps the faces (asymmetric methods only), so check the result (e.g. nx_find_geometry on the new face) when the side matters. On a straight edge between perpendicular faces of length L the removed volume is offset1*offset2/2*L. Returns the feature, the modified body, the offsets used and the measured volume before/after/removed; values stay editable expressions. Edge IDs of the body go stale after the chamfer."""


def nx_variable_blend(edge: str, points: list[dict]):
    """Create a native variable-radius edge blend on one owned edge ID. points: 2-20 items {"parameter": p, "radius": r}; p in [0, 1] is the fraction of arc length along the edge from its NX start vertex (0) to its end vertex (1), r > 0 in part units; parameters must be distinct. NX interpolates the radius smoothly between the points. Returns the blend feature, the modified body, each radius point with its actual 3D position (use it to see which end is the start) and the measured volume before/after."""


def nx_mirror_body_about(body: str, plane: str, keep_original: bool = True):
    """Mirror an owned solid body about a datum plane ID (associative) or a planar face ID (a fixed datum plane is created in the face's plane). Creates a native Mirror Body feature producing a new, separate (not united) body of equal volume. NX's Mirror Body stays associative to its source and cannot delete it, so keep_original=false hides (blanks) the source body instead; it still exists and still counts in part-scope measurements. Returns the feature, the new body ID, the plane origin/normal used and source_hidden."""


def nx_split_body(body: str, tool: str):
    """Split an owned solid body with a datum plane ID or a planar face ID of another body; a face is used as its infinite plane (an associative coincident plane). Creates a native Split Body feature. Returns the feature, every resulting body ID with its volume (normally 2 bodies whose volumes sum to the original) and the plane used; a plane that misses the body or only touches its boundary (all of the body on one side, checked on its bounding box) is rejected with NX_INVALID_ARGUMENT before NX runs; a plane that cuts the box but not the body fails with NX_API_ERROR "Tool body completely outside target body"."""


def nx_trim_body(body: str, tool: str, reverse: bool = False):
    """Trim an owned solid body with a datum plane ID or a planar face ID of another body; a face is used as its infinite plane. NX removes the material on the side the tool normal points to (datum plane normal, or the face's outward normal) and keeps the other side; reverse=true keeps the side the normal points to instead. The kept side is verified from the result's centroid. Creates a native Trim Body feature; returns it with the trimmed body, the volume before/after, the plane origin/normal and kept_side. A plane that does not cut the body is rejected like in nx_split_body (NX_INVALID_ARGUMENT, or NX_API_ERROR from NX when it cuts only the bounding box)."""


def nx_scale_body(bodies: list[str], factor: float, origin: list[float] | None = None):
    """Uniformly scale owned solid bodies about origin [x, y, z] (default the work-part origin) with a native Scale feature. factor > 0 and != 1: every coordinate p becomes origin + factor*(p - origin), so lengths scale by factor and volume by factor^3 (verified). Returns the feature, the scaled bodies (same IDs), factor, origin and the volumes before/after."""

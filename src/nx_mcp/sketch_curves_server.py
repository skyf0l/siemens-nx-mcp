"""Typed schemas: Sketch curves: polygon, ellipse, sketch fillet/chamfer, offset/mirror, construction toggle, project/intersection curves.

Each public function is one MCP tool: its signature is the input schema and its
docstring the description. Handlers are SketchCurvesMixin methods named "_" + name[3:].
READ_ONLY lists tools that never change NX state; NON_MODEL tools change NX
state but not model geometry (views, files). SUMMARIES maps a tool name to a
request_summary template (params, context) -> one line for the NX MCP panel.

Sketch coordinates are local [x, y] in the sketch frame (nx_sketch_info gives
the frame); lengths use work-part units and angles degrees.
"""

from __future__ import annotations

from typing import Literal

READ_ONLY: set[str] = set()
NON_MODEL: set[str] = set()


def _in(c, p):
    return f"sketch {c.obj(p.get('sketch_id'))}"


SUMMARIES: dict = {
    "nx_sketch_polygon": lambda p, c: (
        f"Add a {p.get('sides')}-sided polygon R{c.length(p.get('radius'))} to {_in(c, p)}"
    ),
    "nx_sketch_ellipse": lambda p, c: (
        f"Add an ellipse (semi-axes {c.length(p.get('major_radius'))} x "
        f"{c.length(p.get('minor_radius'))}) to {_in(c, p)}"
    ),
    "nx_sketch_fillet": lambda p, c: (
        f"Fillet two curves R{c.length(p.get('radius'))} in {_in(c, p)}"
    ),
    "nx_sketch_chamfer": lambda p, c: (
        f"Chamfer two curves {c.length(p.get('distance1'))}"
        + (
            f" x {c.length(p.get('distance2'))}"
            if p.get("distance2") not in (None, p.get("distance1"))
            else ""
        )
        + f" in {_in(c, p)}"
    ),
    "nx_sketch_offset": lambda p, c: (
        f"Offset {c.count(p.get('curves'), 'curve')} by {c.length(p.get('distance'))} "
        f"in {_in(c, p)}"
    ),
    "nx_sketch_mirror": lambda p, c: (
        f"Mirror {c.count(p.get('curves'), 'curve')} about {c.obj(p.get('mirror_line'))} "
        f"in {_in(c, p)}"
    ),
    "nx_sketch_construction": lambda p, c: (
        f"Make {c.count(p.get('curves'), 'curve')} "
        f"{'construction' if p.get('construction', True) else 'active'} in {_in(c, p)}"
    ),
    "nx_sketch_project": lambda p, c: (
        f"Project {c.count(p.get('objects'), 'object')} into {_in(c, p)}"
    ),
    "nx_sketch_intersection": lambda p, c: (
        f"Intersect {c.count(p.get('faces'), 'face')} with {_in(c, p)}"
    ),
}


def nx_sketch_polygon(
    sketch_id: str,
    center: list[float],
    sides: int,
    radius: float,
    inscribed: bool = True,
    rotation: float = 0.0,
):
    """Add a closed regular polygon of sides (3-64) straight sketch lines, joined end to end with coincident constraints. center is local [x, y]; radius > 0 in part units. inscribed=True puts the vertices on the radius circle; inscribed=False circumscribes the circle (edge midpoints at radius, vertices at radius/cos(180/sides)). rotation (degrees, counterclockwise from the sketch +x axis) is the direction of the first vertex. Returns the line IDs in counterclockwise order, the actual vertices and edge length read back from NX. Activates the sketch if needed and restores the prior activation."""


def nx_sketch_ellipse(
    sketch_id: str,
    center: list[float],
    major_radius: float,
    minor_radius: float,
    rotation: float = 0.0,
    start_angle: float = 0.0,
    end_angle: float = 360.0,
):
    """Add a native sketch ellipse. center is local [x, y]; major_radius >= minor_radius > 0 (part units) are the semi-axes; rotation (degrees, counterclockwise from sketch +x) is the major-axis direction. start_angle/end_angle (degrees, 0 < end-start <= 360) give an elliptical arc running counterclockwise from start to end; they are polar angles of the end points measured at the center from the major axis (NX convention, not the parametric angle); the default is a full closed ellipse. Returns the curve ID and NX's actual center, radii, rotation, arc length."""


def nx_sketch_fillet(
    sketch_id: str,
    curve1: str,
    curve2: str,
    radius: float,
):
    """Create a native sketch fillet (tangent arc of radius > 0) between two owned sketch lines or arcs, trimming or extending both to the arc ends, like NX's Fillet command with Trim. The fillet is placed in the corner nearest where the curves meet (their closest ends). Returns the new arc ID and its actual center and radius; curve1/curve2 keep their IDs but their endpoints move (re-read with nx_sketch_info). Fails if the curves cannot take that radius."""


def nx_sketch_chamfer(
    sketch_id: str,
    curve1: str,
    curve2: str,
    distance1: float,
    distance2: float | None = None,
):
    """Create a native sketch chamfer line between two owned intersecting sketch lines, trimming both. distance1 > 0 is cut back along curve1 from the corner and distance2 along curve2 (default: distance2 = distance1, a symmetric chamfer). Applied at the corner nearest where the lines meet. Returns the chamfer line ID and its actual endpoints and length."""


def nx_sketch_offset(
    sketch_id: str,
    curves: list[str],
    distance: float,
    side: Literal["auto", "left", "right"] = "auto",
):
    """Create associative native offset copies of owned sketch curves (a chain, a closed loop or single curves) in the same sketch at distance > 0 (part units). side picks the offset direction: left/right relative to the direction of the first curve (start -> end) as seen from the sketch normal; auto keeps NX's default (outward for closed loops). Returns the new curve IDs and their read-back geometry, plus the measured offset distance."""


def nx_sketch_mirror(
    sketch_id: str,
    curves: list[str],
    mirror_line: str,
):
    """Create associative native mirror copies of owned sketch curves about mirror_line, an owned straight sketch line or a datum axis in the sketch plane; the original curves remain. The mirror line is not converted to construction. Returns the mirrored curve IDs and their read-back geometry, verified as reflections of the inputs."""


def nx_sketch_construction(
    sketch_id: str,
    curves: list[str],
    construction: bool = True,
):
    """Convert owned sketch curves to construction (reference) geometry with construction=True, or back to active profile geometry with construction=False. Construction curves stay in the sketch but are ignored by extrude/revolve sections. Returns each curve with its read-back construction state."""


def nx_sketch_project(
    sketch_id: str,
    objects: list[str],
):
    """Project model edges or curves (IDs of kind edge or curve owned by the work part, not in this sketch) onto the sketch plane as associative native projected sketch curves that follow later model changes. Returns the projected curve IDs with read-back geometry; usable as profile curves or references."""


def nx_sketch_intersection(
    sketch_id: str,
    faces: list[str],
):
    """Create associative native intersection curves where model faces (owned by the work part) cut the sketch plane; they update with the model. Faces that do not cross the plane fail. Returns the new sketch curve IDs with read-back geometry."""

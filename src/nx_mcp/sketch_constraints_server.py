"""Typed schemas: Sketch dimensions and constraints between points and curves: point-to-point and two-curve dimensions, midpoint.

Each public function is one MCP tool: its signature is the input schema and its
docstring the description. Handlers are SketchConstraintsMixin methods named "_" + name[3:].
READ_ONLY lists tools that never change NX state; NON_MODEL tools change NX
state but not model geometry (views, files). SUMMARIES maps a tool name to a
request_summary template (params, context) -> one line for the NX MCP panel.
"""

from __future__ import annotations

from typing import Literal

READ_ONLY: set[str] = set()
NON_MODEL: set[str] = set()


def _value(p, c, unit=None):
    value = p.get("value")
    if value is None:
        return "at its current size"
    if not isinstance(value, int | float):
        return f"to {value}"
    return f"to {value:g}{unit}" if unit else f"to {c.length(value)}"


SUMMARIES: dict = {
    "nx_sketch_point_dimension": lambda p, c: (
        f"Dimension {p.get('kind', 'distance')} between {c.obj(p.get('first'))} "
        f"{p.get('first_point')} and {c.obj(p.get('second'))} {p.get('second_point')} "
        + _value(p, c)
    ),
    "nx_sketch_curve_dimension": lambda p, c: (
        f"Dimension {str(p.get('kind', '')).replace('_', ' ')} between {c.obj(p.get('first'))} "
        f"and {c.obj(p.get('second'))} "
        + _value(p, c, " deg" if p.get("kind") == "angle" else None)
    ),
    "nx_sketch_midpoint": lambda p, c: (
        f"Put the {p.get('point')} of {c.obj(p.get('point_curve'))} "
        + (
            "equidistant from the ends of "
            if p.get("on_curve", True) is False
            else "at the midpoint of "
        )
        + c.obj(p.get("target_curve"))
    ),
    "nx_sketch_point_on_curve": lambda p, c: (
        f"Put the {p.get('point')} of {c.obj(p.get('point_curve'))} on {c.obj(p.get('curve'))}"
    ),
}


def nx_sketch_point_dimension(
    sketch_id: str,
    first: str,
    first_point: Literal["start", "end", "center", "mid"],
    second: str,
    second_point: Literal["start", "end", "center", "mid"],
    kind: Literal["distance", "horizontal", "vertical"] = "distance",
    value: float | None = None,
):
    """Create a native driving sketch dimension between a point of one sketch curve and a point of another (or the same) curve of the sketch. Points: start/end of a line or arc, center of an arc/circle, mid of a line or arc. kind: distance = true point-to-point distance, horizontal/vertical = distance along the sketch X/Y axis. value (positive, work-part length units) drives the geometry to that size; value=None measures the current size and fixes it (must be nonzero). The NX solver may move either curve. Returns the dimension constraint ID, its expression (ID and name; edit later with nx_set_expression), the requested and measured value read back from the solved geometry, both point coordinates (sketch-local and work part) and the sketch solver status. Over-constrained or unsatisfied results roll back."""


def nx_sketch_curve_dimension(
    sketch_id: str,
    first: str,
    second: str,
    kind: Literal["angle", "parallel_distance", "perpendicular_distance"],
    value: float | None = None,
    second_point: Literal["start", "end", "center", "mid"] | None = None,
):
    """Create a native driving sketch dimension between two different curves of one sketch. angle: two lines, value in degrees (0 < value < 180), measured between the line directions start->end (NX may report the supplementary angle; the readback gives the angle actually solved). parallel_distance: perpendicular gap between two parallel lines (make them parallel first, e.g. nx_sketch_relation parallel). perpendicular_distance: distance from line `first` to a point of curve `second` measured perpendicular to the line; second_point selects that point (default center, so `second` is then an arc/circle). Lengths in work-part units. value=None measures the current size and fixes it; a value drives the geometry to it. Returns the dimension constraint ID, its expression (edit later with nx_set_expression), the value measured back from the solved geometry and the sketch solver status. Conflicting or unsatisfied results roll back."""


def nx_sketch_midpoint(
    sketch_id: str,
    point_curve: str,
    point: Literal["start", "end", "center"],
    target_curve: str,
    on_curve: bool = True,
):
    """Constrain a curve point (start/end of a line or arc, or an arc/circle center) to the midpoint of another line or arc in the same sketch. NX's native midpoint constraint alone only makes the point equidistant from the target's two ends (it may sit anywhere on the perpendicular bisector); with on_curve=true (default) a native point-on-curve constraint is added as well so the point lands exactly on the midpoint. on_curve=false keeps only the equidistance constraint. The point is moved by the sketch solver; the target may move too if it is not otherwise constrained. Returns the created constraint IDs, the point and midpoint coordinates and residuals read back after the solve, and the sketch solver status. Fully closed circles have no ends and are rejected as targets."""


def nx_sketch_point_on_curve(
    sketch_id: str,
    point_curve: str,
    point: Literal["start", "end", "center"],
    curve: str,
):
    """Constrain a curve point (start/end of a line or arc, or an arc/circle center) to lie on another line, arc or circle of the same sketch (native point-on-curve; a line counts as its infinite extension). The solver moves the point to the closest valid location. Returns the constraint ID, the point and its distance to the curve read back after the solve, and the sketch solver status. Use nx_sketch_relation coincident to join two curve points."""

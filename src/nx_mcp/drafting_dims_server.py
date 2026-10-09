"""Typed schemas: Drawing dimensions beyond linear (radial, diameter, angular, ordinate) and drawing standard (projection angle, templates).

Each public function is one MCP tool: its signature is the input schema and its
docstring the description. Handlers are DraftingDimsMixin methods named "_" + name[3:].
READ_ONLY lists tools that never change NX state; NON_MODEL tools change NX
state but not model geometry (views, files). SUMMARIES maps a tool name to a
request_summary template (params, context) -> one line for the NX MCP panel.
"""

from __future__ import annotations

from typing import Annotated, Literal

from nx_mcp.schema_types import Field

Point2 = Annotated[list[float], Field(min_length=2, max_length=2)]

READ_ONLY: set[str] = set()
NON_MODEL: set[str] = set()
SUMMARIES: dict = {
    "nx_add_radial_dimension": lambda p, c: (
        f"{'Diameter' if p.get('kind') == 'diameter' else 'Radius'} dimension on "
        f"{c.obj(p.get('edge'))}"
    ),
    "nx_add_angular_dimension": lambda p, c: (
        f"Angle dimension between {c.obj(p.get('first'))} and {c.obj(p.get('second'))}"
    ),
    "nx_add_ordinate_dimensions": lambda p, c: (
        f"{c.count(p.get('objects'), 'ordinate dimension')} "
        f"({p.get('direction', 'horizontal')}) from {c.obj(p.get('origin_object'))}"
    ),
    "nx_create_drawing_sheet": lambda p, c: (
        f"Drawing sheet {p.get('name', 'Sheet1')}, "
        f"{(p.get('projection') or 'third_angle').replace('_', ' ')} projection"
        + (" from template" if p.get("template") else "")
    ),
}


def nx_add_radial_dimension(
    view: str,
    edge: str,
    kind: Literal["radius", "diameter"] = "radius",
    origin: Point2 | None = None,
):
    """Create a native associative radius (R) or diameter (Ø) dimension on a circular or arc edge shown in a drawing view. view is a drawing-view ID; edge is an owned (or work-assembly occurrence) circular edge ID, e.g. from nx_find_geometry(kind="edge", geometry_type="circle"), seen true-shape in the view (the view looks along the arc's axis; otherwise NX_UNSUPPORTED_ARGUMENT). origin=[x,y] places the dimension text in sheet units (default: up-right of the arc center at 45 degrees, about 12 mm (drawing scale 1) outside the arc). Returns the dimension ID, NX's computed value in model units (radius or diameter), the edge's model radius and the placement; the value is verified against the edge geometry."""


def nx_add_angular_dimension(
    view: str,
    first: str,
    second: str,
    origin: Point2 | None = None,
):
    """Create a native associative angular dimension between two straight edges shown in a drawing view (e.g. a chamfer face edge and a side edge). view is a drawing-view ID; first and second are owned (or work-assembly occurrence) linear edge IDs that are not parallel in the view. origin=[x,y] (sheet units) places the dimension text and chooses which of the angle's quadrants is dimensioned (default: 10 mm right of and above the point midway between the two edges' midpoints in the view). Returns the dimension ID, NX's computed angle in degrees, the projected angle between the edges in the view (degrees, 0-90 acute value) and the placement; NX's value must equal that angle or its supplement."""


def nx_add_ordinate_dimensions(
    view: str,
    origin_object: str,
    objects: Annotated[list[str], Field(min_length=1, max_length=50)],
    direction: Literal["horizontal", "vertical"] = "horizontal",
    origin_snap: Literal["start", "end", "center"] = "start",
    object_snap: Literal["auto", "start", "end", "center"] = "auto",
    margin: float | None = None,
):
    """Create native associative ordinate dimensions in a drawing view, all measured from one new ordinate origin (datum) along the view's horizontal or vertical axis. origin_object is an edge ID; origin_snap picks its start/end vertex or its arc center as the datum point (a full circle has no end vertices: use center). objects are 1-50 unique edge IDs; object_snap auto measures circular edges at their center and other edges at their start vertex (or force start/end/center; start/end are rejected on full circles). The dimensions sit on one ordinate margin line: for horizontal ordinates margin is the line's sheet y in sheet units (default 15 mm above the view border), for vertical ordinates its sheet x (default 15 mm left of the view). Returns the ordinate origin ID, the margin and, per object, the dimension ID, snap, model point and NX's computed ordinate (model units, unsigned) verified against the model geometry."""


def nx_create_drawing_sheet(
    name: str = "Sheet1",
    size: Literal["A0", "A1", "A2", "A3", "A4"] | None = None,
    scale: float = 1.0,
    units: Literal["mm", "in"] = "mm",
    projection: Literal["first_angle", "third_angle"] = "third_angle",
    template: str | None = None,
):
    """Create and open a native drawing sheet with an explicit projection angle (first_angle = ISO/European, a view placed right of its parent shows the part's left side; third_angle = ASME/US, it shows the right side). Without template: landscape A0-A4 (default A3) custom-size sheet in mm or in sheet units and positive scale. With template: the sheet, border and title block are copied from an NX drawing sheet template, given as an installed template name from UGII_BASE_DIR/DRAFTING/templates (e.g. "Drawing-A3-Size2D-template" or "A3"; a name without a folder is always looked up there) or a workspace-relative .prt path that contains a folder (e.g. "templates/my_a3.prt"); the template defines the size (size, if given, must match) and units. Returns the sheet ID, actual size, scale, units, projection read back from NX and the template used."""

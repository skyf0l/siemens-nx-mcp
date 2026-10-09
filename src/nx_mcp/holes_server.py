"""Typed schemas: Native hole features on faces: simple, counterbore, countersink, threaded.

Each public function is one MCP tool: its signature is the input schema and its
docstring the description. Handlers are HolesMixin methods named "_" + name[3:].
READ_ONLY lists tools that never change NX state; NON_MODEL tools change NX
state but not model geometry (views, files). SUMMARIES maps a tool name to a
request_summary template (params, context) -> one line for the NX MCP panel.
Lengths are work-part units, angles degrees, points [x, y, z] in the work part.
"""

from __future__ import annotations

from typing import Literal

READ_ONLY: set[str] = set()
NON_MODEL: set[str] = set()


def _hole_summary(p, c):
    positions = p.get("positions") or []
    count = len(positions) if isinstance(positions, list) else 0
    form = p.get("form", "simple")
    size = p.get("thread_size") if form == "threaded" else c.length(p.get("diameter"))
    extent = "through all" if p.get("through_all") else f"{c.length(p.get('depth'))} deep"
    return f"{form.capitalize()} hole {size} x{count}, {extent}, on {c.obj(p.get('face'))}"


SUMMARIES: dict = {"nx_hole_feature": _hole_summary}


def nx_hole_feature(
    face: str,
    positions: list[list[float]],
    form: Literal["simple", "counterbore", "countersink", "threaded"],
    diameter: float,
    depth: float | None = None,
    through_all: bool = False,
    tip_angle: float = 118.0,
    counterbore_diameter: float | None = None,
    counterbore_depth: float | None = None,
    countersink_diameter: float | None = None,
    countersink_angle: float = 90.0,
    thread_standard: str | None = None,
    thread_size: str | None = None,
    thread_depth: float | None = None,
    target_body: str | None = None,
):
    """Create one native NX Hole feature (the modern Hole command, NXOpen HolePackage) with 1-100 holes on a planar face.

    face: planar face ID owned by the work part (placement face). positions: 1-100 hole centres [x, y, z] in work-part coordinates; each must lie on the face's plane (tolerance 1e-4 x the largest coordinate, min 1e-4; projected onto it) and strictly inside the face boundary, and positions must be distinct. Holes are drilled into the material, i.e. opposite the face's outward normal; the cut is a subtract from the face's body (target_body, if given, must be that body).

    Depth: give depth (> 0, cylindrical depth from the placement face to the shoulder, excluding the drill tip) or through_all=true (through the whole body, no tip), not both. tip_angle (degrees, 0-180 exclusive, default 118; 0 is not allowed) shapes the cone at the bottom of a blind hole. Every form measures depth from the placement face.

    form:
    - simple: diameter.
    - counterbore: diameter (hole), counterbore_diameter > diameter, counterbore_depth > 0 and < depth.
    - countersink: diameter (hole), countersink_diameter > diameter, countersink_angle (included angle in degrees, 0-180 exclusive, default 90); the countersink cone height (countersink_diameter - diameter) / 2 / tan(angle / 2) must be < depth.
    - threaded: thread_standard and thread_size name a row of the installed NX thread table (list them with nx_thread_catalog, e.g. "Metric Coarse" / "M10 x 1.5"); diameter is the tap-drill diameter and must equal one of that size's catalog TapDrillDia values (which selects the radial engagement), thread_depth (> 0, <= depth; default = depth, or the full length for through_all) is the threaded length. Metric standards need a mm part, inch standards an inch part. The hole carries native NX thread data (symbolic, right-hand; no chamfers or relief).

    Returns the hole feature, the modified body, the actual hole parameters read back from NX, the drill direction, the number of holes, the measured removed_volume (cubic work-part units) and the nominal removed volume of one hole when it lies fully inside the material (blind holes only; tip cone included). removed_matches_nominal is false when the measured removal differs from the nominal total (a blind hole breaks out of the body or overlaps another cut), null for through holes."""

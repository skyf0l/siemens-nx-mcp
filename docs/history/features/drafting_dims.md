# Drafting dimensions and drawing standard (`drafting_dims`)

Radial, diameter, angular and ordinate drawing dimensions, and drawing sheets
with an explicit projection angle, optionally from an NX sheet template.
Schemas: `src/nx_mcp/drafting_dims_server.py`; handlers:
`src/nx_mcp/drafting_dims.py`; live test: `acceptance/live/drafting_dims.json`.

All placement coordinates are drawing-sheet coordinates in the sheet's units
(mm or in). Dimension values are in model units (degrees for angles). Every
dimension is associative to model edges, so the value follows model edits, and
every handler compares NX's computed value with the model geometry and raises
`NX_VERIFICATION_FAILED` (rolled back) on mismatch.

## nx_add_radial_dimension(view, edge, kind="radius", origin=None)
Radius (R) or diameter (Ø) dimension on a circular edge or arc visible in a
drawing view. Uses `RadialDimensionBuilder` with measurement method
`Radial`/`Diametral`. `origin` defaults to a point up-right of the arc center
at 45 degrees, 12 mm outside the arc (correct on sheets in either unit). The arc
must be seen true-shape (the view looks along its axis); an arc seen edge-on or
oblique returns `NX_UNSUPPORTED_ARGUMENT`. Returns `measured_value`,
`expected_value`, `edge_radius`, `edge_center`.

```json
{"tool": "nx_add_radial_dimension", "args": {"view": "<view id>", "edge": "<circle edge id>", "kind": "diameter", "origin": [70, 228]}}
```
Verified: 12 mm hole in a top view -> R6.0 and Ø12.0. A straight edge gives
`NX_OBJECT_TYPE_MISMATCH`.

## nx_add_angular_dimension(view, first, second, origin=None)
Angle between two straight edges as seen in a view (`AngularDimensionBuilder`,
creates a `MinorAngularDimension`). The response also gives `view_angle_deg`
(acute angle between the projected edges) and `supplement_deg`; NX dimensions
either the angle or its supplement depending on which quadrant `origin` lies
in. Edges seen end-on or parallel in the view are rejected. The default origin
is 10 mm right of and above the point midway between the edges' midpoints.

Verified: slanted edge (atan(20/10)) vs back edge -> 116.565° (= 180 − 63.435);
3 mm chamfer edge vs top face in the right view -> 135° (= 180 − 45).

## nx_add_ordinate_dimensions(view, origin_object, objects, direction="horizontal", origin_snap="start", object_snap="auto", margin=None)
Ordinate dimensions from one new ordinate origin. The origin is the start/end
vertex or the arc center of `origin_object`; each of the 1-50 `objects` is
measured at its arc center (circular edges) or start vertex (`auto`), or at a
forced snap. `start`/`end` on a full circle are rejected with
`NX_OBJECT_TYPE_MISMATCH` (NX measures a closed circle at its center). Dimensions
sit on one ordinate margin line: `margin` is the line's sheet y for horizontal
ordinates (default 15 mm above the view border) or sheet x for vertical
ordinates (default 15 mm left of the view), in sheet units. Values are unsigned
distances along the view's X or Y axis.

Implementation: the UF drafting calls `CreateOrdorigin` -> `CreateOrdinateMargin`
-> `CreateOrddimension`. On NX 2406 the `OrdinateDimensionBuilder` commits only
the ordinate origin plus zero-valued placeholder dimensions when no explicit
margin exists, so it is not used. Each call creates its own ordinate origin.

Verified: origin (0,0,0); horizontal: hole 20, slant start 60; vertical: hole
20, back edge 40.

## nx_create_drawing_sheet(name="Sheet1", size=None, scale=1.0, units="mm", projection="third_angle", template=None)
New, opened drawing sheet with explicit projection: `third_angle` (ASME: a view
projected to the right of its parent shows the right side) or `first_angle`
(ISO: it shows the left side). Without a template: custom landscape A0-A4
(default A3). With `template`: an installed NX sheet template name (`"A3"` ->
`Drawing-A3-Size2D-template.prt`, or the file stem) from
`UGII_BASE_DIR/DRAFTING/templates` (a name without a folder is always looked up
there), or a workspace-relative `.prt` path containing a folder (e.g.
`templates/my_a3.prt`; outside paths give `NX_PATH_OUTSIDE_WORKSPACE`). The
template part's units must match the work part's: metric templates (A0-A4) in
mm parts, inch templates (A-F, e.g. `Drawing-B-Size2D-template` with
`units="in"`) in inch parts; otherwise `NX_UNSUPPORTED_ARGUMENT`. The template supplies size, border and title block; if
`size` is given it must match. The sheet is renamed to `name`, and scale and
projection are applied explicitly. The response includes `template_projection`
and `warnings` when the template's static projection symbol or title-block
scale text no longer match.

Independently verified (inch A4, A0, `.prt`-suffixed and non-2D template
names, workspace template path, B-size inch template in an inch part).

Verified: third-angle sheet: right projected view looks from +X, bottom view
from −Y. First-angle sheet: right projected view looks from −X. Template "A3"
gives an A3 sheet with the Siemens border and title block. Unknown template
gives `NX_NOT_FOUND`.

## Known limits
- Template projection symbols and title-block text are static template
  graphics. NX does not update them when the projection or scale changes (the
  tool warns).
- Unit frames: `UF_VIEW_map_model_to_drawing` and `UF_DRAW_ask_view_borders`
  answer in sheet units, while NXOpen origins and `UF_DRF` placement points
  take part units. Default placements are computed in sheet units and converted
  once (verified on an inch sheet of a mm part).
- Topology IDs (edges/faces) go stale after every successful mutation, and a
  rolled-back `NX_VERIFICATION_FAILED` invalidates all references (views too):
  re-find geometry right before each call.
- Angular dimensions: the quadrant (angle or supplement) comes from the text
  origin. There is no explicit acute/obtuse switch.
- Ordinate dimensions: no dogleg handling. Objects with the same coordinate
  overlap on the margin.

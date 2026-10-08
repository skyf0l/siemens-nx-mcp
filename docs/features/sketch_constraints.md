# Sketch constraints: point/curve dimensions, midpoint, point on curve

Handlers: `src/nx_mcp/sketch_constraints.py` (`SketchConstraintsMixin`).
Schemas: `src/nx_mcp/sketch_constraints_server.py`.
Live test: `acceptance/live/sketch_constraints.json` (NX 2406).

All four tools take a sketch ID and curve IDs owned by that sketch (lines, arcs,
circles), activate the sketch if needed (and restore activation), and return the
created constraint IDs, the geometry read back after the solve and the native
solver status (`solver_status`, `remaining_degrees_of_freedom`). If NX cannot
satisfy the request, the sketch becomes OverConstrained/InconsistentlyConstrained,
or an existing constraint disappears, the call fails and the undo mark rolls
everything back.

Point selectors: `start`/`end` of a line or arc, `center` of an arc or circle,
`mid` of a line or arc (dimensions only). A full circle has only `center`.

## nx_sketch_point_dimension

Driving dimension between two curve points (they may belong to the same curve).

- `kind`: `distance` (true distance), `horizontal` / `vertical` (along the sketch
  X / Y axis).
- `value`: positive length in work-part units drives the geometry; `None`
  measures the current size and fixes it (zero size is rejected).
- Returns `constraint`, `expression` (`object.id`, `name` like `p0`, value; edit
  later with `nx_set_expression`), `value_before`, `measured_value` (read back
  from the solved geometry), both points (sketch-local and work-part
  coordinates) and the solver status.

```json
{"tool": "nx_sketch_point_dimension", "args": {"sketch_id": "<sk>", "first": "<line1>",
 "first_point": "end", "second": "<line3>", "second_point": "start",
 "kind": "horizontal", "value": 25}}
```

Verified on NX 2406: horizontal 20→25 mm, vertical measured-and-fixed 5 mm, line
length 26.93→30 mm via its end points, arc center to line `mid` measured
61.27 mm; arc start/end chord 10 mm (probe). The solver may move either curve
(a `fix` constraint on a line did not stop its end point from moving).

## nx_sketch_curve_dimension

Dimension between two different curves; `first` must be a line.

- `angle`: two lines, degrees, 0 < value < 180. NX measures between the line
  directions start→end (value=None returns that angle); the readback also
  accepts the supplementary angle.
- `parallel_distance`: gap between two parallel lines (rejected before NX if they
  are not parallel; make them parallel first, e.g. `nx_sketch_relation parallel`).
  Natively a perpendicular dimension from line `first` to the start point of
  `second` (NX rejects line-to-line without a point: "associativity type is invalid").
- `perpendicular_distance`: from line `first` to a point of `second`
  (`second_point`, default `center`, i.e. an arc/circle center).

Verified: angle 56.44→60°, parallel distance 20→15 mm then 15→18 mm through its
expression, line to arc center 10→12 mm.

## nx_sketch_midpoint

Puts `point` of `point_curve` on the midpoint of `target_curve` (line or open arc).

NX's native **midpoint constraint only makes the point equidistant from the
target's two ends** (it may sit anywhere on the perpendicular bisector). With
`on_curve=true` (default) a native point-on-curve constraint is added as well, so
the point lands exactly at the midpoint (two constraint IDs). `on_curve=false`
keeps only the equidistance constraint. Returns the residuals (`equidistance`,
`to_midpoint`).

Verified: circle center to the midpoint of a line (followed a later edit of the
line's position), line end to the bisector of a half arc.

## nx_sketch_point_on_curve

Puts `point` of `point_curve` on a line (infinite extension), arc or circle.
Verified: line start on another line's extension, line end on a circle.
Coincident point-to-point joins remain `nx_sketch_relation coincident`.

## Why midpoint was rejected before, and NX quirks (NX 2406, modern solver)

- `Sketch.CreateMidpointConstraint` / `CreatePointOnCurveConstraint` /
  `CreateCoincidentConstraint` create persistent constraints but **do not move
  geometry**: `Sketch.Update()`, `LocalUpdate()`, `Update(geoms)`,
  deactivate/reactivate, `UpdateManager.DoUpdate` and `SketchConstraintBuilder`
  with `UpdateSketchAtCommit` all leave the curves where they were, while the
  status still reports UnderConstrained. Only a driving change (a dimension
  expression edited to a new value, or a sketch drag) runs the solver. A
  residual check of such a constraint therefore fails, and the midpoint
  constraint is also not "on the midpoint" (see above). These tools create the
  constraints and then solve with a native `SketchDragGeometryBuilder` drag of
  the constrained point to its target (`Sketch.SketchGeometry.PointType` uses the
  `Sketch.PointType` enum: StartPoint/EndPoint/Center, not ConstraintPointType).
- `ConstraintPointType.MidVertex` as a midpoint target fails with
  "Invalid object type"; the target is the curve itself (`NotSet`).
- Dimensions solve on `EditExpression` + `Sketch.Update()`. A duplicate dimension
  on the same point pair raises "Persistent relation already exists"
  (nx_code 910023) → `NX_SKETCH_CONFLICT`. An unreachable driving value leaves
  the geometry unchanged with status OverConstrained → `NX_SKETCH_CONFLICT`.
- Any rolled-back call makes the part's object IDs (even the sketch ID) stale;
  re-find them with `nx_list_sketches` + `nx_sketch_info`.

## Known limits

- `nx_set_expression`, `nx_sketch_diagnostics`, `nx_sketch_conflicts` and
  `nx_sketch_relation` currently fail on NX 2406 once a sketch has a dimension:
  the shared `_expression_record` calls `Expression.GetExpressionParents`, which
  does not exist in 2406. These tools therefore use their own solver-status
  check and expression record; the acceptance file ends with those shared tools
  and passes completely only after the shared fix.
- Splines, ellipses and other curve types are rejected.
- Dimension annotation placement is automatic (cosmetic only).

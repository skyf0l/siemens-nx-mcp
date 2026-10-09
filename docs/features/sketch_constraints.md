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

- The shared `_expression_record` now tolerates the missing
  `Expression.GetExpressionParents` on NX 2406: `nx_set_expression`,
  `nx_sketch_diagnostics`, `nx_sketch_conflicts`, `nx_sketch_dimension` and
  `nx_feature_parameters` work on dimensioned sketches (independent verification,
  see below). These tools still use their own solver-status check.
- `nx_set_expression` on a dimension of a sketch that is still active returns
  the new value, but the curves only move when the sketch is finished
  (`nx_finish_sketch`) or solved again; finish the sketch before reading geometry
  back after an expression edit.
- `nx_sketch_midpoint` / `nx_sketch_point_on_curve` solve by a native drag. In a
  heavily constrained sketch the drag can fail (`drag_solver_status`
  `UpdateFailed`); the call then rolls back with `NX_CONSTRAINT_UNSATISFIED`.
  Example: an arc start onto a line extension when the arc centre is already on
  another line and its end is equidistance-constrained.
- NX's `fix` constraint on a line fixes the infinite line, not its end points: a
  dimension to a "fixed" line's end may slide that end along the line.
- The solver may also move the target (e.g. the arc moved 60 mm to the circle
  centre for a midpoint constraint); constrain what must stay put.
- Splines, ellipses and other curve types are rejected.
- Dimension annotation placement is automatic (cosmetic only).

## Independent verification (verify_b, NX 2406)

- Replay of `acceptance/live/sketch_constraints.json` on a fresh part: all 38
  steps as expected, including `nx_sketch_diagnostics`, `nx_sketch_conflicts`
  and `nx_set_expression`.
- XZ-plane sketch (sketch-local frame != part frame): line start to arc mid
  measured 60.8276 (= |(60,10)|); arc chord horizontal 17.3205 -> 20; line mid
  vertical 17.5 -> 12; circle centre to line end 44.7214 -> 35; angle measured
  26.5651 and driven to 135 (obtuse, read back 135.000000); perpendicular_distance
  with second_point end 63.2456 -> 50, arc start 0 -> 8, line mid 25 -> 40.
  Expression values equal the read-back geometry; work-part coordinates have
  y = 0 and z = sketch y.
- Inch part: horizontal 1.5 in, parallel_distance 0.75 in, perpendicular 0.4 in,
  expressions in `in`.
- Midpoint: line start to line midpoint (20, 0), circle centre to arc midpoint,
  on_curve=false (point projected onto the bisector x+y=110, residual 0).
  Point-on-curve onto circle, arc, line extension, arc centre onto a line.
- 24 rejected inputs (line centre, circle start/end, same point/curve, value 0
  or negative, zero size measured, angle >= 180, non-line operands, second_point
  with angle, parallel lines without an angle value, full-circle midpoint
  target, curves of another sketch -> NX_OBJECT_OWNER_MISMATCH, wrong object
  kinds -> NX_OBJECT_TYPE_MISMATCH): sketch geometry and constraint list
  unchanged afterwards.

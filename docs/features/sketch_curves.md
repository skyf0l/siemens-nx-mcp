# Sketch curves

Group `sketch_curves`: schemas in `src/nx_mcp/sketch_curves_server.py`, NX-side
handlers in `src/nx_mcp/sketch_curves.py` (`SketchCurvesMixin`), local tests in
`tests/test_sketch_curves.py`, live acceptance in
`acceptance/live/sketch_curves.json`.

Common rules:

- Coordinates are sketch-local `[x, y]` in the frame returned by
  `nx_create_sketch` / `nx_sketch_info`. Lengths use work-part units and angles
  are in degrees.
- The sketch must belong to the work part. If another sketch is active, the
  call fails with `NX_SKETCH_ACTIVE`. An inactive target sketch is activated
  for the call and deactivated afterwards.
- Every result lists the new curves (`curves`, `curve_ids`, kind `curve`). Each
  curve is read back from NX with its local `start`/`end`, `length` and
  `closed` flag, plus `center`/`radius` for arcs and
  `center`/`major_radius`/`minor_radius`/`rotation`/`start_angle`/`end_angle`
  for ellipses. Ellipse angles are reported in the sketch frame (NX stores them
  relative to the curve's own matrix, which a mirror flips).
- Results are verified against the request. A mismatch raises
  `NX_VERIFICATION_FAILED`, and the executor rolls the operation back.
- A rejected call that changed nothing keeps object IDs
  (`"references": "preserved"`). A rollback after NX already built something
  invalidates the part's IDs (`"references": "invalidated"`); re-acquire the
  sketch by name with `nx_sketch_info` and use the curve IDs it returns.
- `nx_sketch_info` reads ellipses (center, major_radius, minor_radius); for
  splines (for example ellipse offsets) it reports only the type.

## nx_sketch_polygon(sketch_id, center, sides, radius, inscribed=True, rotation=0)

Adds a regular polygon with 3–64 sides, built from sketch lines joined by
coincident constraints. With `inscribed` the vertices sit on the radius circle.
Otherwise the polygon circumscribes the circle, so the edge midpoints sit at the
radius. `rotation` is the direction of the first vertex. The lines are returned
counterclockwise, together with the read-back `vertices` and `edge_length`.

Example: `{"center": [0, 0], "sides": 6, "radius": 20}` gives side 20.

**Verified (NX 2406):**
- Hexagon R20 extruded 10 gives volume 10392.305 (exact).
- Circumscribed square (R10, rotation 45) is axis-aligned 20x20; extruded 10 it
  gives 4000.0.

## nx_sketch_ellipse(sketch_id, center, major_radius, minor_radius, rotation=0, start_angle=0, end_angle=360)

Adds a native `NXOpen.Ellipse` (created with `Curves.CreateEllipse` using the
sketch orientation). `rotation` is the direction of the major axis.
`start_angle`/`end_angle` define an elliptical arc running counterclockwise;
they are **polar** angles of the end points measured at the center from the
major axis (NX convention, not the parametric angle). The defaults give a full,
closed ellipse. Full ellipses are checked against the exact perimeter (AGM);
arcs are checked at both end points.

**Verified:**
- 20x10 ellipse at 30°: center, radii and rotation read back exactly; perimeter
  96.884482.
- Thin ellipses 50x5 and 50x1: lengths 203.198709 and 200.191958, equal to the
  exact perimeter (the former Ramanujan II check was off by 1.2e-5 / 1.6e-4
  and rejected them).
- 20x10 rotated 90°, -45..45°: ends (308.944, 108.944) and (291.056, 108.944)
  around center (300, 100), length 30.385206 (polar angles, radius 12.649).
- Rotation 120 reads back 120; 90..450 gives a closed ellipse; 10x10 works.
- Extruded 10, volume is 6283.355 against the analytic 6283.185 (+2.7e-5, NX
  body tolerance).
- Half ellipse 10x5, 0–180°: ends at (±10, y), length 24.2211.

`nx_sketch_info` now reports sketches with ellipses (center, major_radius,
minor_radius), verified on six ellipses in one sketch.

## nx_sketch_fillet(sketch_id, curve1, curve2, radius)

Calls native `Sketch.Fillet` with trim on. NX adds coincident and tangent
constraints and no radius dimension. The fillet goes in the corner where the
curves' nearest ends meet. Both input curves keep their IDs; the result
includes their trimmed geometry.

Both arc ends must lie on the original curves. Otherwise the call is rejected,
because NX would silently extend the inputs when the radius is too large for
the corner.

**Verified:**
- R5 on a 40x30 rectangle corner: arc center (85, 5) and lines trimmed to 35 and
  25.
- R100 on the same kind of corner is rejected with `NX_VERIFICATION_FAILED` and
  the rectangle is restored.
- R14.9 works.
- Line–arc: R2 between a line and an R10 arc gives center (28.168, 2), tangent
  to the line and 12.0 from the arc center.

**Known limits:** for curves that cross, the result depends on which ends are
nearest. Tangent inputs (an arc continuing into a line) have no corner: NX
builds a full circle and the call is rejected with `NX_VERIFICATION_FAILED`.

## nx_sketch_chamfer(sketch_id, curve1, curve2, distance1, distance2=None)

Uses `SketchChamferBuilder` (Symmetric or Asymmetric) with trim on. A help point
inside the corner selects the quadrant to cut. NX assigns Distance1 to a line by
its own rule, not by selection order. When the read-back shows the distances
swapped, the tool undoes the attempt (inner invisible mark) and repeats it with
the values exchanged. The chamfer is then verified: each end lies on its
original line, at `distance1` along curve1 and `distance2` along curve2.

Supported for lines only (arcs/ellipses: `NX_OBJECT_TYPE_MISMATCH`). Lines
that do not meet are rejected with `NX_VERIFICATION_FAILED` (NX would extend
them to their virtual corner).

**Verified:**
- 4/6 chamfer at (50, 30): ends at (54, 30) and (50, 24).
- Symmetric 3 at (0, 30): ends (3, 30)/(0, 27), length 4.242641; asymmetric
  2 along the left line / 7 along the bottom line at (0, 0): (0, 2)/(7, 0).
- Rectangle with R5 fillet and 4/6 chamfer extruded 10 gives volume 11826.350
  (exact).

## nx_sketch_offset(sketch_id, curves, distance, side="auto")

Creates an associative `SketchOffsetBuilder` offset with a dimension, 1 copy and
arc caps. `side` is `left` or `right` of the first curve's direction as seen
from the sketch normal; `auto` keeps NX's default, which is outward for a
counterclockwise closed loop. Every output is checked to lie at `distance` from
the inputs (UF minimum distance). The result reports `measured_distance` and
`side`. Lines and arcs are offset exactly; ellipses and splines become splines
built with an approximation tolerance of 0.001 mm (the builder default 0.0
failed with "Cannot approximate offset curve", nx_code 1720072), and their
measured distance is checked to that tolerance.

**Verified:**
- Hexagon R10, offset 2 (auto): vertex radius 12.3094, side `right` (outward).
- Hexagon R10 (counterclockwise) offset 2 `left` = inward, vertex radius
  7.690599; `right` = outward, 12.309401.
- R10 arc (0..90°) offset 3 `left`: R7, same center.
- Ellipse 20x10 offset 2: spline starting (22, 100) (auto/right, measured
  1.99943) and (18, 100) (left, measured 1.99992).
- Line along +x, offset 3: `left` gives y = -57, `right` gives y = -63.

## nx_sketch_mirror(sketch_id, curves, mirror_line)

Creates an associative `SketchMirrorPatternBuilder` mirror about a line of this
sketch or a datum axis parallel to the sketch plane (projected onto it). A line
of another sketch is rejected with `NX_OBJECT_OWNER_MISMATCH`; a datum axis
normal to the plane with `NX_INVALID_ARGUMENT`. The mirror line is not
converted to construction. Each output is verified to be the reflection of one
input.

**Verified:**
- 6 hexagon lines mirrored about x = 60 land at the expected reflected
  endpoints.
- A line and 2 arcs mirrored about a datum axis (the Y axis) are exact
  reflections.
- Mirror about a Y-parallel datum axis 50 above the sketch: (0,-40)-(30,-40)
  becomes (0,-40)-(-30,-40).
- Ellipse arc 10x4, rotation 30, 0..200°, center (320, 0) mirrored about x=300:
  center (280, 0), rotation 150, reflected ends (271.340, 5) and
  (285.059, -6.030).

## nx_sketch_construction(sketch_id, curves, construction=True)

Uses `ConvertToFromReferenceBuilder` (Reference/Active) and reads the result
back with `Curve.IsReference`.

**Verified:**
- A line toggled to construction, back to active, and to construction again
  reads back correctly each time.
- The extrude ignores the construction line (volume unchanged).

## nx_sketch_project(sketch_id, objects)

Uses an associative `SketchProjectBuilder` with Original curve type. Accepts
edges and curves owned by the work part that are not in this sketch. Returns
the projected curves and the projection feature.

**Verified:**
- A block's top edge projected into a sketch 20 above it gives the line
  (140, -10)–(160, -10).
- The line is still correct after the block height changes from 10 to 15.
- A cylinder's circular edge projects to an R5 arc at (150, 0); an ellipse of
  another sketch projects to a 20x10 ellipse.
- An edge normal to the sketch plane (it would project to a point) is rejected
  with `NX_INVALID_ARGUMENT` (nx_code 674275); faces are rejected with
  `NX_OBJECT_TYPE_MISMATCH`, curves already in the sketch with
  `NX_INVALID_ARGUMENT`.

## nx_sketch_intersection(sketch_id, faces)

Uses an associative `SketchIntersectionCurveBuilder`. Every output must lie in
the sketch plane.

**Verified:**
- Block top face against an XZ sketch at y = 0 gives the line (140, 10)–(160, 10).
- After the block extrude is edited from 10 to 15, the curve follows to z = 15.
- Cylinder side face at z = 5 gives an R5 circle at (150, 0); an elliptic
  extrude side face gives the 20x10 ellipse.
- A face that does not cross the plane is rejected with `NX_INVALID_ARGUMENT`
  (nx_code 3680011); edges with `NX_OBJECT_TYPE_MISMATCH`.

## NX API notes (2406)

- `UF.Modeling` has no `AskCurveProps` in Python. Use `UF.Eval` instead:
  `Initialize`, `AskLimits`, `EvaluateUnitVectors`, `EvaluateClosestPoint`.
- Builder factories need typed nulls, not `None`: `SketchOffset.Null`,
  `SketchPattern.Null`, `Features.Feature.Null`, `SketchIntersectionCurve.Null`.
- `SketchProjectBuilder` raises "Invalid data" unless `Tolerance` and the
  section tolerances are set.
- `SketchOffsetBuilder.ReverseOffsetDirection()` raises "Sketch is not
  initialized"; `ReverseOffsetDirectionOfChain(curve)` works.
- `SketchOffsetBuilder.Tolerance` defaults to 0.0; set it before
  `UpdateLoopsAndCopies()` or offsets of ellipses/splines fail.
- `Ellipse.StartAngle/EndAngle` are polar angles relative to the curve's own
  matrix; a mirrored ellipse gets its own matrix (rotation 0, flipped normal).

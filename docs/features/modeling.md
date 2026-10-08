# Modeling group: datum features, sketches on faces/datums, circular patterns

Schemas: `src/nx_mcp/modeling_server.py`. Handlers: `src/nx_mcp/datum_features.py`
(`DatumFeaturesMixin`). Local tests: `tests/test_datum_features.py`. Live
acceptance: `acceptance/live/modeling.json` (run with `scripts/dev_call.py`
while holding the NX lock; delete `smoke/modeling/w1_acceptance.prt` or change
the path before re-running).

All lengths are in work-part units, angles in degrees, vectors `[x, y, z]` in
the work-part frame. Every handler reads back what NX built and raises
`NX_VERIFICATION_FAILED` on a mismatch, so the operation's undo mark rolls it
back. All six tools were verified live on NX 2406 (see
`capability_fragments/modeling.json` for the exact scope).

## nx_datum_plane
Native datum plane feature.
- `method="offset"`: `reference` = planar face ID or datum plane ID, `distance`
  signed along the reference normal (a face's outward normal). Negative
  distance or `flip=true` places it on the other side; the result normal equals
  the reference normal. Associative; the distance is an editable expression.
- `method="point_normal"`: `origin`, `normal` (fixed plane).
- `method="three_points"`: `points` = three non-collinear points; normal =
  (p1-p0) x (p2-p0) (fixed plane).
- `name` optional feature name.

Returns `feature`, `datum_plane` (ID usable by `nx_create_sketch_on` and as
an offset reference), actual `origin` and `normal`.

Example: `{"method": "offset", "reference": "<top face id>", "distance": 15}`
on a 10 mm high block -> origin z=25, normal +Z.

Verified: offsets from top face (+15 -> z=25; flip 4 and -4 -> z=6), bottom
face (+5 -> z=-5, normal -Z), from a datum plane (+5 -> z=30); point_normal;
three_points normal (-0.2425, 0, 0.9701); expression edit moved the plane.

Limits: `point_normal` / `three_points` planes are fixed (not associative to
other geometry).

## nx_datum_axis
- `method="point_direction"`: `origin`, `direction`.
- `method="two_points"`: `start`, `end` (direction start -> end).

Returns `feature`, `datum_axis` (ID usable as `nx_circular_pattern` axis),
actual `origin` (a point on the line; NX extends the axis beyond the given
points) and unit `direction`, verified for line and sense.

Limits: creates fixed (`FIXED_DATUM_AXIS`) features.

## nx_datum_csys
`origin`, unit orthogonal `x_axis` and `y_axis` (z = x cross y), optional
`name`. Returns the feature, the coordinate system ID and the read-back frame.
Verified: CSYS at (10,5,10) rotated 30 deg about Z.

## nx_datum_point
`position`, optional `name`. Returns the point feature and actual coordinates.

## nx_create_sketch_on
`target` = planar face ID or datum plane ID owned by the work part,
optional `name`, optional `x_direction` (must lie in the plane). Creates and
activates an associative sketch; finish it with `nx_finish_sketch`.
- Without `x_direction`: NX chooses the horizontal axis; the origin is the
  part origin projected onto the plane (e.g. top face z=10 -> origin (0,0,10),
  x=+X, so sketch coordinates equal world XY).
- With `x_direction`: x axis is exactly that vector, y = normal x x, origin is
  the target's reference point (face point / datum plane origin), returned in
  `frame`.

Sketch `[u, v]` maps to `origin + u*x_axis + v*y_axis`.

Example: datum plane at z=25 with origin (20,10,25), `x_direction=[0,1,0]`
-> y_axis (-1,0,0); a circle at local (0,-16) lies at world (36,10,25).

Verified: sketches on a face and on a datum plane (with and without
`x_direction`), circles extruded as unite boss (+141.3717 mm3), unite post
(+188.4956 mm3) and new body (bounding box exactly as predicted); sketches
followed face/plane edits.

Limits: face IDs go stale after any model mutation (executor policy) —
re-acquire with `nx_find_geometry` right before use. Another active sketch
must be finished first (`NX_SKETCH_ACTIVE`).

## nx_circular_pattern
`features` (owned feature IDs, e.g. extrude boss/cut), `count` (total
instances incl. seed, 2-360), `angle` (pitch, degrees), and either `axis`
(datum axis ID, associative) or `axis_origin` + `axis_direction`.
`(count-1)*angle` must be < 360. Uses NX Pattern Feature, Circular, Count and
Pitch (simple method).

Example: `{"features": ["<boss extrude>"], "count": 6, "angle": 60, "axis":
"<datum axis id>"}`.

Verified: 6 x 60 deg of a boss about a datum axis (+5 x 28.2743 mm3 exact)
and of a hole about an explicit axis (-5 x 12.5664 mm3 exact); rejected
request left the volume unchanged.

Limits: rotation sense for asymmetric spans not separately verified; instances
are not checked individually beyond the resulting volume (callers should
measure).

## NX 2406 API notes
- `Plane.SetFlip` on an offset datum plane reverses only the plane normal, not
  the offset side; the signed expression is used instead.
- `DatumAxisBuilder` uses `Point1`/`Point2` for two points (no `StartPoint`).
- `SketchInPlaceBuilder2`: `PlaneOption=Inferred` + `PlaneOrFace.Value` is
  associative but ignores `AxisReference`. `PlaneOption=NewPlane` with a
  coincident smart plane honours the plane only when `AxisReference` (and
  `OriginOption=SpecifyPoint` + `SketchOrigin`) are set; without them the
  sketch silently lands on the absolute XY plane.
- Circular pattern: `AngularSpacing.SpaceType = PatternSpacing.SpacingType.Offset`
  (Count and Pitch), `NCopies` includes the seed; datum-axis direction via
  `Directions.CreateDirection(datum_axis, Sense.Forward, ...)`.

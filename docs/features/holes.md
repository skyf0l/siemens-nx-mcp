# Holes: native NX Hole features

Group `holes`: schema in `src/nx_mcp/holes_server.py`, NX-side handler in
`src/nx_mcp/holes.py` (`HolesMixin`), local tests in `tests/test_holes.py`, live
acceptance in `acceptance/live/holes.json`.

## nx_hole_feature

Creates one native NX **Hole** feature (the modern Hole command,
`NXOpen.Features.HolePackageBuilder` / `HolePackage`, feature type
`HOLE PACKAGE`) with 1-100 holes on one planar face. The legacy `nx_hole` stays
a plain cylinder subtract; use `nx_hole_feature` for real holes (drill tip,
counterbore, countersink, tapped threads, hole callouts).

### Arguments

| Argument | Meaning |
| --- | --- |
| `face` | Planar face ID owned by the work part (placement face). Non-planar faces fail with `NX_NON_PLANAR`. |
| `positions` | 1-100 distinct hole centres `[x, y, z]` in work-part coordinates. Each must lie on the face plane (tolerance 1e-4 x the largest coordinate magnitude, at least 1e-4) and inside the face boundary (UF point containment; points on the boundary are rejected). Points within tolerance are projected onto the plane. |
| `form` | `simple`, `counterbore`, `countersink` or `threaded`. |
| `diameter` | Hole (drill) diameter. For `threaded` it is the tap-drill diameter and must equal one of the catalog `TapDrillDia` values of the size; it selects the radial engagement row. The error lists the valid values. |
| `depth` | Cylindrical depth measured from the placement face to the shoulder (NX "Depth to cylinder bottom"); the drill tip comes on top. Give `depth` or `through_all=true`, not both. |
| `through_all` | Through the whole body (NX "Through Body"); no tip. |
| `tip_angle` | Drill-tip included angle, degrees, 0 < angle < 180, default 118. |
| `counterbore_diameter`, `counterbore_depth` | Counterbore only: diameter > `diameter`, depth < `depth`. |
| `countersink_diameter`, `countersink_angle` | Countersink only: diameter > `diameter`, included angle default 90; the countersink cone height `(Dcs - d) / 2 / tan(angle / 2)` must be < `depth`. |
| `thread_standard`, `thread_size` | Threaded only: a row of the installed NX thread table (`UGII/modeling_standards/NX_Thread_Standard.xml`, the same table `nx_thread_catalog` / `nx_standard_thread` read), e.g. `"Metric Coarse"` / `"M10 x 1.5"`. Metric standards need a mm part, inch standards an inch part. |
| `thread_depth` | Threaded only: thread length, <= `depth`. Default: `depth` for blind holes, full length for `through_all`. |
| `target_body` | Optional; must be the body that owns `face` (the cut is always a subtract from that body). |

Direction is always into the material: opposite the face's outward normal
(`UF_MODL_ask_face_props` normal at the face's mid-parameter).

### Result

Hole feature and modified body references, `parameters` read back from the
committed feature (form, diameter, depth or `null` for through, tip angle,
counterbore/countersink values, thread standard/size/radial engagement/thread
depth), `hole_count`, NX's hole `positions` (`HolePackage.GetOrigins`),
`direction`, `removed_volume` (measured body volume change),
`nominal_volume_per_hole` / `nominal_volume_total` (analytic, blind holes only;
valid when holes lie fully in material and do not overlap),
`removed_matches_nominal` (`false` when a blind hole breaks out of the body or
overlaps another cut, `null` for through holes), and for threaded
holes a `thread` block (standard, size, radial engagement, tap drill, thread
depth, major/minor diameter, pitch, angle, callout from the catalog row).

The handler verifies after commit: one origin per requested position, every
direction equals the drill direction, read-back parameters equal the request,
the body lost volume. Any mismatch raises `NX_VERIFICATION_FAILED` and the
operation's undo mark rolls the feature back.

### Example

```json
{"tool": "nx_hole_feature", "args": {
  "face": "<top face id>", "positions": [[40, 10, 20], [40, 30, 20]],
  "form": "threaded", "diameter": 6.8, "depth": 15,
  "thread_standard": "Metric Coarse", "thread_size": "M8 x 1.25", "thread_depth": 12}}
```

### Verified scope (NX 2406, `acceptance/live/holes.json`)

60 x 40 x 20 mm block (48000 mm³):

| Call | Removed (NX) | Analytic |
| --- | --- | --- |
| 2 x simple 6 x 10, tip 118 | 599.4645 | 2 x (π·9·10 + π·9·1.80258/3) = 599.4645 |
| counterbore 11 x 4 + 6 x 12 | 623.3163 | π(30.25·4 + 9·8) + 16.9889 = 623.3163 |
| countersink 12 x 90° + 6 x 12 | 469.3783 | frustum π·3/3·(36+18+9) + π·9·9 + 16.9889 = 469.3783 |
| 2 x threaded M8 x 1.25, drill 6.8 x 15, thread 12 | 1138.9660 | 1138.9660 |
| through-all 5 on the +X face | 1178.0972 | π·6.25·60 |
| through-all M6 x 1.0 (drill 5), full thread, on the -Y face | 785.3982 | π·6.25·40 |

- Drill directions read back as -Z, -X and +Y respectively (into the material).
- `nx_recognize_holes` found all 9 bores (counterbore = 2 coaxial bores, 8 groups).
- Threaded holes carry native thread data: string expressions for standard,
  size and radial engagement and numeric expressions major 8, minor 6.619,
  pitch 1.25, angle 60 (`nx_feature_parameters`).
- Rejected (NX_INVALID_ARGUMENT, `mutation_outcome: rolled_back`, volume
  unchanged at 43205.3794 mm³, `nx_model_health` healthy): a position 1 mm
  above the face, a position on the plane but outside the face, a non-catalog
  tap drill (7 for M8 x 1.25; valid 6.8, 7.2).

### Known limits

- Planar placement faces only; one face (and therefore one body) per call.
- Thread representation is NX's symbolic hole thread data, not helical
  geometry; right-hand threads only; start/end chamfers and relief disabled;
  the `Custom` radial-engagement rows are not selectable.
- Holes that break out of the body (blind depth beyond the far face, or a
  hole near a side face) are built by NX; their nominal volumes do not apply and
  `removed_matches_nominal` is `false`.
- Not live-verified: `Custom` radial engagement, non-planar or occurrence faces.
- Holes are fixed to the given coordinates (smart points), not dimensioned to
  edges in a sketch.
- After any rejected call, re-acquire body/face IDs (the rollback invalidates
  them), as the acceptance file does.

## Independent verification (verify_b, NX 2406)

- Replay of `acceptance/live/holes.json` on a fresh part: identical volumes
  (599.4645, 623.3163, 469.3783, 1138.9660, 1178.0972, 785.3982 mm³).
- 60 x 40 x 20 mm block: 5 simple 3 x 8 holes with tip 90 removed 300.4148
  (analytic 300.4148); counterbore 9 x 3 + 5 through 524.6460 (π·(20.25·3 +
  6.25·17)); countersink 8 x 90 + 4 through 284.8377; M5 x 0.8 through with
  thread_depth 8 removed 277.0885 (π·4.41·20), thread_length "value" 8; blind
  M6 x 1.0 (drill 5 x 10, thread depth defaulted to 10) 206.1811.
- Breakout: a 4 x 30 blind hole in the 20 mm block removed 251.3274 (π·4·20)
  against nominal 382.0249; a 6 mm hole 2 mm from a side face removed 142.1568
  of 158.3606; `nx_model_health` healthy.
- Inclined faces (block on a plane tilted 30° about X): 2 x 6 x 10 removed
  599.4645, counterbore through on the opposite face 524.6460, M8 x 1.25 blind
  460.5326; drill directions (0, 0.5, -0.866) and (0, -0.5, 0.866).
- Inch part (3 x 2 x 1 in): 2 x 0.25 x 0.5 simple 0.0515453 in³, counterbore
  0.4375 x 0.25 + 0.25 x 0.6 0.0559921, countersink 0.5 x 82° through 0.0584988,
  1/4-20 UNC tap drill 0.201 x 0.6 thread 0.5 0.0196772 (all equal to analytic);
  metric thread on the inch part -> NX_UNSUPPORTED_ARGUMENT.
- target_body: the other body -> NX_INVALID_ARGUMENT; the owning body accepted
  (through 4 mm in a 10 mm body: 125.6637).
- Rejected with stable codes, volume and feature count unchanged, model healthy:
  cylindrical face (NX_NON_PLANAR), body ID as face (NX_OBJECT_TYPE_MISMATCH),
  position on the boundary or inside an existing hole, duplicates, 101
  positions, depth + through_all, neither, tip 0/180, negative diameter,
  counterbore depth >= depth / diameter <= hole / missing depth, countersink
  cone deeper than depth, countersink angle 180, thread args on a simple hole,
  unknown standard/size (NX_NOT_FOUND), inch standard on mm part
  (NX_UNSUPPORTED_ARGUMENT), thread_depth > depth, 0.01 off-plane. A position
  0.00005 off the plane is projected and accepted.

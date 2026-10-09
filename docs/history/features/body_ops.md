# Body operations (group `body_ops`)

Schemas: `src/nx_mcp/body_ops_server.py`. Handlers: `src/nx_mcp/body_ops.py`
(`BodyOpsMixin`). Local tests: `tests/test_body_ops.py`. Live acceptance:
`acceptance/live/body_ops.json` (6 new parts under `smoke/body_ops/*_acceptance.prt`;
run with `scripts/dev_call.py`; rename the parts to rerun because nx_create_part
refuses existing files).

Lengths are in work-part units, angles in degrees, and points are `[x, y, z]` in
the work part. Every tool validates its arguments before calling NXOpen. It
measures the body with NX mass properties before and after the operation. If
NX's result differs from the request, the tool raises `NX_VERIFICATION_FAILED`
and the operation's undo mark rolls the change back. Face and edge IDs of a
changed body go stale, so find them again with `nx_find_geometry` or
`nx_list_topology`.

All six tools were verified on NX 2406 (status `tested`). The numbers below come
from the acceptance run.

## nx_chamfer_advanced(edges, method, offset1, offset2=None, angle=None, reverse=False)
- **Purpose:** native Chamfer feature with symmetric, two-offset or offset-angle
  sections. The existing `nx_chamfer` is symmetric only.
- **Arguments:**
  - `symmetric` takes only `offset1`.
  - `two_offsets` needs `offset1` and `offset2`, both > 0.
  - `offset_angle` needs `offset1` and `angle`, with 0 < angle < 90. The other
    face is cut back by `offset1*tan(angle)`.
  - `reverse` swaps which face takes `offset1` (asymmetric methods only).
  - All edges must belong to one owned solid body.
- **Example:**
  `{"edges": ["obj_…"], "method": "two_offsets", "offset1": 2, "offset2": 4}`
- **Returns:** the feature, the body, the offsets, `second_offset_effective`, and
  `volume_before` / `volume_after` / `volume_removed`.
- **Verified:** on a 40x20x10 block:

  | Method | Edge length | Offsets | Removed volume |
  | --- | --- | --- | --- |
  | two_offsets | 40 | 2 / 4 | 160.000 |
  | offset_angle | 40 | 3 at 30° | 103.923 |
  | two_offsets, reverse | 20 | 1 / 5 | 50.000 |
  | symmetric | 20 | 2 | 40.000 |

  Each result equals `o1*o2/2*L`. With `reverse=true` on a parallel edge, the
  faces swap: top/side cut back by 2/6 became 6/2.
- **Limits:** NX chooses the "first" face from the edge orientation. If the side
  matters, check the new face afterwards.

## nx_variable_blend(edge, points)
- **Purpose:** variable-radius Edge Blend on one owned edge.
- **Arguments:** `points` has 2-20 items of `{parameter: 0..1, radius > 0}`.
  `parameter` is the fraction of arc length from the edge's NX start vertex.
  Parameters must be distinct.
- **Example:**
  `{"edge": "obj_…", "points": [{"parameter": 0, "radius": 2}, {"parameter": 1, "radius": 4}]}`
- **Returns:** the feature, the body, each point with its actual 3D position, and
  the volume before/after.
- **Verified:**
  - Radii 2→4 on a 40 mm edge removed 80.122 mm³. A linear radius predicts 80.13;
    constant r=2 and r=4 give bounds of 34.3 and 137.3.
  - Three points with radii 1/3/1 removed 46.698 (bounds 8.6 to 77.3).
- **API:** `EdgeBlendBuilder.AddChainset` followed by
  `AddVariableRadiusDataNew(edge, "<percent>", "<radius>", point, False, False)`.
  The point is created with `PointOnCurveLocationOption.PercentArcLength`.
- **Independent verification:** radii 1→3 on a 40 mm edge removed 37.200,
  which equals the linear-radius value. On a closed R10 circle edge, p=0 r=1
  and p=0.5 r=2 removed 30.827, with p=0.5 placed diametrically opposite.
- **Limits:** the radius at each point is not read back individually. On a
  closed edge p=0 and p=1 are the same point; using both fails with NX 670075.
  A radius too large for the adjacent faces fails with NX 3620008. The model
  is unchanged after either failure.

## nx_mirror_body_about(body, plane, keep_original=True)
- **Purpose:** native Mirror Body about a datum plane ID (associative) or a planar
  face ID. For a face, a fixed datum plane is created in the face's plane. The
  existing `nx_mirror_body` handles only XY/XZ/YZ.
- **Returns:** the feature, the new body, `plane_origin` / `plane_normal`,
  `plane_kind`, `source_hidden` and `source_volume`. The mirrored volume is
  verified to equal the source.
- **Verified:**
  - Mirror about the datum plane x=50: new body bounding box x 60..100, y 0..20,
    z 0..10 (exact reflection). Volume 8000; part total 16000.
  - Mirror about the face y=20 with `keep_original=false`: bounding box y 20..40,
    volume 8000.
- **Limits:**
  - `MirrorBodyBuilder` (NX 2406) has no delete-source option, and the feature
    stays associative to its source. So `keep_original=false` hides (blanks) the
    source body. It still exists and still counts in part-scope measurements.
  - Oblique plane (independent verification): mirroring about the plane
    through (50,0,0) with normal (1,1,0) gave bounding box x 30..50, y 10..50,
    z 0..10, which is the exact reflection.

## nx_split_body(body, tool)
- **Purpose:** native Split Body by a datum plane ID, or by a planar face of
  another body. The face is used as an associative coincident infinite plane.
- **Returns:** the feature, every resulting body with its volume, and the plane.
  The tool verifies at least 2 bodies whose volumes sum to the original.
- **Verified:**
  - Datum plane z=4 split the block into 4800 + 3200.
  - Face x=110 of another body split a block into 6000 + 2000.
- **Limits:**
  - NX does not extend a selected face on its own. Using it directly fails with
    "Tool body completely outside target body", which is why the face's plane is
    used.
  - A plane that misses the body or only touches its boundary is rejected with
    `NX_INVALID_ARGUMENT` before NX runs. The check is against the body's
    bounding box. Before this check, NX failed with 670030, and the
    rolled-back failure left its message on the next feature created, so
    `nx_model_health` reported a false error. A plane that cuts the box but not
    the body still reaches NX and returns 670030.
  - A face of the body being split is rejected.

## nx_trim_body(body, tool, reverse=False)
- **Purpose:** native Trim Body by a datum plane ID or by a planar face of another
  body, used as an infinite plane.
- **Side rule:** NX removes the material on the side the tool normal points to:
  the datum normal, or the face's outward normal. `reverse=true` keeps that side
  instead. The kept side is checked from the result's centroid.
- **Verified:**

  | Tool | Normal | reverse | Kept | Volume |
  | --- | --- | --- | --- | --- |
  | Datum plane x=10 | +X | false | x 0..10 | 2000 |
  | Datum plane x=110 | +X | true | x 110..140 | 6000 |
  | Face x=215 of another body | −X | false | x 215..240 | 5000 |
  | Oblique datum (120,10,0), normal (1,1,0) | (1,1,0) | false | x+y ≤ 130 | 4000 |
  | Oblique datum (220,10,0), normal (1,1,0) | (1,1,0) | true | x+y ≥ 230 | 4000 |

  The two oblique rows come from independent verification.
- **Limits:** a plane that does not cut the body is rejected as in
  `nx_split_body`.

## nx_scale_body(bodies, factor, origin=[0,0,0])
- **Purpose:** native uniform Scale feature on owned solid bodies. Each point p
  maps to `origin + factor*(p - origin)`.
- **Returns:** the feature, the bodies (same IDs), the factor, the origin and the
  volumes before/after. The tool verifies the volume ratio is `factor^3`.
- **Verified:**
  - x2 about the origin: 8000 → 64000, bounding box 0..80 / 0..40 / 0..20.
  - x0.5 about [140,20,10]: 8000 → 1000, bounding box x 120..140, y 10..20,
    z 5..10.
  - Independent verification: two 10 mm cubes scaled x3 together about
    [10,10,10] became 27000 each, with bounding boxes -20..10 and x 40..70.
    Factor 1, 0 and -2, a 2-element origin, duplicate bodies and an edge ID are
    all rejected with the model unchanged.
- **Limits:**
  - `factor = 1` is rejected.
  - Non-uniform and axisymmetric scaling are not offered.
- **API:** `ScaleBuilder.BodyToScale.Add(bodies)`; `BodyCollector` is null.

## NX 2406 API notes
- `ChamferBuilder.ChamferOption`: `SymmetricOffsets`, `TwoOffsets`,
  `OffsetAndAngle`. The values go in `FirstOffsetExp`, `SecondOffsetExp` and
  `AngleExp`; `ReverseOffsets` swaps the faces.
- `SplitBodyBuilder` / `TrimBody2Builder`:
  - `TargetBodyCollector` starts as None, so assign a collector to it.
  - `BooleanTool.FacePlaneTool.ToolFaces.FaceCollector` exists and is read-only,
    so call `ReplaceRules` on it. Use `CreateRuleFaceDatum` for datum planes.
  - For faces, use `ToolOption = NewPlane` plus `FacePlaneTool.ToolPlane` set to a
    coincident `Plane`.
- `CreateTrimBody2Builder` and the other builders reject `Feature.Null`; pass
  `None`.
- `MirrorBodyBuilder.Plane` is a `SelectDatumPlane`, so it needs a DatumPlane, not
  a face. `MirrorBodyBuilder` has no `DeleteSourceBody`.
- For a planar face, the direction returned by `UF Modeling.AskFaceData` is
  already the outward normal. Do not multiply it by the trailing `norm_dir`
  value.

# NX 2406 core-tool regression (verify_core)

Pre-existing tools (validated only on NX 2606) re-run on the live NX 2406
development session through the MCP (`scripts/dev_call.py`, `nx_invoke`, agent
surface), after a `dev_reload.py` of the current working tree. Parts are under
`<workspace>/smoke/verify_core/`. Every result was checked numerically
(volume, exact bounds, sketch read-back, component poses).

Summary: 65 tools run, 62 PASS, 3 FAIL. 8 tools NOT RUN (drawing and sheet
metal). The NX session stopped responding before those sessions: NX sat idle
with a modal "Work in Progress" window, the dev journal stopped answering
(dev-state generation 62), and every call returned `NX_BRIDGE_UNAVAILABLE`.

## Part lifecycle

| Tool | Result | Evidence / error | Suspected cause |
|---|---|---|---|
| nx_create_part | PASS | life.prt etc. created, mm | |
| nx_save_part | PASS | file saved and verified | warning text says "NX v2606" (cosmetic) |
| nx_save_as | PASS | life_copy.prt became the work part, modified=false | |
| nx_export_step | PASS | life.step, 7972 B, AP214, 1 solid | |
| nx_close_part | PASS | closed_count 1, part gone from the list | |
| nx_open_part | PASS | reopened life.prt, volume 1000 | |
| nx_activate_part | PASS | life work+display, volume 1000 | |
| nx_list_open_parts | PASS | compact/active_only filters correct | |
| nx_import_geometry | **FAIL** | `NX_IMPORT_FAILED`, nx_code 4625016 "Unable to import selected file to work part", no translator files (also fails with the source part closed) | `hardened._import_geometry_inner` never sets `Step214Importer.OutputFile`. NX 2406 rejects WorkPart import without it. A dev_python probe with `OutputFile` set imported 1 body, 1000 mm³, bounds [0,0,0]-[20,10,5] |

## Sketch

| Tool | Result | Evidence |
|---|---|---|
| nx_create_sketch | PASS | XY/XZ frames correct |
| nx_sketch_line | PASS | read-back endpoints |
| nx_sketch_constraint | PASS | horizontal (10,1)->(15,0) after dim; perpendicular residual 0 |
| nx_sketch_dimension | PASS | length 15 -> line (0,0)-(15,0); radius 7 -> arc r 7.0 |
| nx_sketch_arc | PASS | full circle r5 and 90° arc r4 |
| nx_sketch_primitive | PASS | circle r3, slot 12x4, rounded rect 20x10 r2; extrudes 589.699 and 89.133 mm³ (exact) |
| nx_sketch_info | PASS | 17 then 18 curves, coordinates correct |
| nx_finish_sketch | PASS | |
| nx_edit_sketch | PASS | line end moved to (0,12); add_line added (100,0)-(110,0) |

## Modeling

| Tool | Result | Evidence |
|---|---|---|
| nx_extrude | PASS | 1000; through_all subtract 960; unite 1460; intersect 460 + bounds 5x10x10; symmetric 8 -> z -4..4, 128 |
| nx_revolve | PASS | 2356.194 (= π·75·10), bounds ±10 |
| nx_sweep | PASS | 40 |
| nx_loft | PASS | 653.333 |
| nx_blend | PASS | 997.854 |
| nx_chamfer | PASS | 995 |
| nx_shell | PASS | 424 |
| nx_draft | PASS | 956.256 (Δ 43.744 = 500·tan 5°) |
| nx_hole | PASS | 984.292 (1000-5π) |
| nx_pattern | PASS | count 2 pitch 3 -> 968.584, second bore at x=8 |
| nx_boolean | PASS | subtract -> 500, bounds 0..5 |
| nx_mirror_body | PASS | 2000, mirror bounds x -10..0 |
| nx_transform_bodies | PASS | copy +20 X, 2000, bounds 20..30, distance 10 |
| nx_thicken | PASS | sheet face, offset 2 -> 200, y -2..0 |
| nx_thread | PASS | symbolic, pitch 0.4, volume unchanged 984.292 |
| nx_standard_thread | PASS | M6x1.0 CUT detailed, 1803.650 -> 1770.337 |

## Expressions and features

| Tool | Result | Evidence |
|---|---|---|
| nx_list_features | PASS | 4 features, 3 after delete |
| nx_feature_parameters | PASS | p0=0, p1=10 mm |
| nx_set_feature_parameters | PASS | p1=22 -> z max 22 |
| nx_edit_feature | PASS | distance 12 -> z 12, also by name |
| nx_set_expression | PASS | p1=15 -> z 15; create vc_len 7 mm. An invalid formula is rejected and the model is unchanged, but the error is generic `NX_API_ERROR` (nx_code 1050007) |
| nx_list_expressions | PASS | name_contains filter |
| nx_checkpoint / nx_rollback | PASS | 3000 -> rollback -> 1500 (IDs stale afterwards, as documented) |
| nx_undo | PASS | 2000 -> undo -> 1500 |
| nx_delete_feature | PASS | body removed, expressions deleted |

## Inspection and view

| Tool | Result | Evidence |
|---|---|---|
| nx_list_bodies | PASS | 2 bodies |
| nx_list_topology | PASS | 6 faces / 12 edges |
| nx_measure_volume | PASS | per body and total |
| nx_mass_properties | PASS | V 1e-6 m³, area 7e-4 m², CoG [10,5,2.5] mm |
| nx_get_bounding_box | PASS | exact and conservative |
| nx_measure_distance | PASS | body-body 10, face-body 10, component-component 5 |
| nx_measure_angle | PASS | 90.0 |
| nx_model_health | PASS | healthy |
| nx_check_interference | PASS | penetration 500 mm³; clear 10; component pair clear 5 |
| nx_recognize_holes | PASS | r 1/2 bores at correct axes |
| nx_set_view / nx_fit_view | PASS | top, isometric |
| nx_set_display | PASS | blue (211), transparency 30 |
| nx_screenshot | PASS | PNG correct; requested 800x500 ignored (1439x1794), which is documented as advisory |
| nx_render_view | PASS | 800x500 PNG, correct image |

## Assembly

| Tool | Result | Evidence / error | Suspected cause |
|---|---|---|---|
| nx_add_component | PASS | poses [0,0,0], [30,0,0], [0,40,0] | |
| nx_list_components | PASS | | |
| nx_assembly_constraint | PASS | fix A; distance 5 opposite -> B at x=15, gap 5 | |
| nx_reposition_component | **FAIL** (partial) | dx-only works (50->55). Any rotation of a component not at the origin -> `NX_PLACEMENT_MISMATCH`, rolled back. rz 90 at the origin works | see below |
| nx_set_component_transform | **FAIL** (partial) | translation-only works. Changing rotation when the current position is not 0 -> `NX_PLACEMENT_MISMATCH` | `hardened._set_component_transform` assumes MoveComponent rotates about the component origin. Probe on 2406: zero shift + rz 90 moved [20,0,0] to [0,20,0], so it rotates about the assembly origin. Fix: `shift = t - delta·p` |

Side effect: after any failed and rolled-back mutation in an assembly, every
component ID goes stale (`NX_OBJECT_STALE`) and a re-list returns new IDs. The
error response does not tell the caller to reacquire references.

## Not run (NX unavailable)

nx_create_drawing, nx_add_base_view, nx_add_projection_view, nx_add_dimension,
nx_list_dimensions, nx_export_drawing_pdf, nx_sheet_metal_schema,
nx_sheet_metal_feature. The step files are ready under the verifier scratch
folder (s8.json, s9.json).

## Follow-up: orchestrator fixes (code-reviewed; live check pending)

After the restart, NX hung again with the modal "Work in Progress" window, this
time over verify_a's nx_create_part. `dev_reload` timed out, the sidecar lost
its connection on the first extrude of s8.json, and the orchestrator stopped
all NX calls. So no fix has been verified live yet. Offline review:

| Fix | Offline result |
|---|---|
| STEP import: `builder.OutputFile = import_dir/"output.prt"` (hardened.py) | Matches the 2406 probe that imported correctly (1000 mm³) |
| Placement pivot: `shift = t - delta·p` (hardened.py) | Matches the measured 2406 behaviour: MoveComponent with zero shift and rz 90 moved [20,0,0] to [0,20,0], which is delta·p. The `rows`/`_nx_matrix` conventions are consistent |
| Invalid formula -> `NX_INVALID_FORMULA` (authoring.py `_native_formula`) | Wraps the native error, keeps nx_code, runs inside the rolled-back mutation |
| Capabilities scoped to version (`manifest.scope_to_version(..., "v2406")`) | nx_extrude tested (verified_versions v2406); nx_datum_plane tested (real_NX_v2406_scoped); nx_bridge_surface "experimental", "Not tested on NX v2406; evidence: ...". Still pending live: nx_capabilities needs the bridge, and the running NX must report the version string "v2406" |

Note: under this gating, nx_import_geometry, nx_set_component_transform,
nx_reposition_component, nx_create_drawing and nx_sheet_metal_feature now
report "experimental" on 2406 until v2406 is added to their verified_versions.

Pending live runs (step files in the verifier scratch folder): s8.json
(drawings), s9.json (sheet metal), s10.json (the four fixes above with
expected numbers: import 1000 mm³; rz 90 of a component at [30,10,0] -> pose
unchanged, assembly bounds x 20..30, y 10..20; ry 90 at [50,-5,0] -> x 50..60,
z -10..0; bad formula -> NX_INVALID_FORMULA, volume stays 1000).

## Recommended shared fixes (hardened.py)

1. STEP import: set `builder.OutputFile` (for example `import_dir/"output.prt"`)
   before `Commit()`.
2. Component placement: compensate the pivot,
   `shift = t - matmul(delta, xyz(p))`, and add a test that rotates an
   off-origin component.
3. Version-neutral warnings: replace the "NX v2606" save/export/translator
   strings and the `Designcenter2606` UGII_BASE_DIR fallback.
4. Map NX expression-formula errors (nx_code 1050007) to a stable domain code.
5. Add a reacquire hint to rolled-back errors that invalidate occurrence IDs.

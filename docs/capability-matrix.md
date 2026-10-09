# Capability evidence matrix

Generated from `src/nx_mcp/capability_manifest.json` plus `src/nx_mcp/capability_fragments/` (`nx_mcp.manifest.load_manifest()`); do not edit this table by hand.
Run `python scripts/generate_capability_matrix.py` to regenerate, or add `--check` to detect drift.

Manifest revision: **2606-manufacturing-r1**. NX: **v2606**. Bridge protocol: **1**.
Canonical manifest SHA-256: `3369a64e0409c26c0de006463c92f12637e6dbab7cc1c86b1bc57406a023371d`.

These labels report manifest evidence, not certification or independent verification of its claims. Native-tested means status `tested` with an evidence type beginning `real_NX_`; only the stated scope and NX version are covered. Contract/sidecar-tested does not establish native CAD correctness. Experimental includes untested entries and tested entries without a recognized evidence type. Unavailable capabilities are explicitly recorded by the manifest; absence from this matrix is not proof of availability or unavailability.

| Tool classification | Count |
| --- | ---: |
| Native-tested | 101 |
| Contract/sidecar-tested | 4 |
| Experimental | 0 |
| Unavailable | 0 |

## Tools

| Tool | Classification | Manifest status | Evidence type | Tested scope / caveat |
| --- | --- | --- | --- | --- |
| nx_add_component | Native-tested | tested | real_NX_v2606 | Initial placement, typed references and nested source assembly |
| nx_assembly_constraint | Native-tested | tested | real_NX_v2606_scoped | Native fix and face-distance constraints; actual separation measured. Other exposed relation types retain narrower validation. |
| nx_batch | Native-tested | tested | real_NX_v2606 | Two-step partial failure fully rolled back; serial execution on journal thread |
| nx_bridge_surface | Native-tested | tested | real_NX_v2606_scoped | Native full-edge planar G0/G1/G2 bridge creation; requested constraints are not independent geometric certification. |
| nx_cancel_operation | Contract/sidecar-tested | tested | local_contract_tests | Sidecar contract accepts cancellation only for running batches and creates the cancellation marker; batch boundary seam verifies rollback. Live non-running request rejected. No mid-NXOpen interruption claim. |
| nx_capabilities | Native-tested | tested | real_NX_v2606_scoped | Live exact-tool manifest filtering, NX version and API-detection response. API presence is not a geometry correctness certificate. |
| nx_check_clearance | Native-tested | tested | real_NX_v2606_interactive | Native 2 mm gap flagged below 3 mm requirement; bounded pair selection and conservative broad phase |
| nx_check_interference | Native-tested | tested | real_NX_v2606_interactive | 10 mm cubes: 500 mm^3 overlap, touching and 2 mm gap; rotated nested occurrence overlap; cleanup preserves saved flags and checkpoint |
| nx_checkpoint | Native-tested | tested | real_NX_v2606 | In-session model checkpoint and available-state inspection |
| nx_checkpoint_state | Native-tested | tested | real_NX_v2606 | Checks actual NX mark availability, including save expiration |
| nx_clear_highlights | Native-tested | tested | real_NX_v2606_public_MCP | Clears MCP-owned native highlights without persistent appearance changes |
| nx_component_action | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_component_array | Native-tested | tested | real_NX_v2606_scoped | Native associative rectangular 3x2 and circular 4-instance patterns, including seed. |
| nx_create_explosion | Native-tested | tested | real_NX_v2606_scoped | Native nested assembly explosion: absolute rotated parent/child poses, reset, repeat assignment, model/drawing association, persistence; ordinary assembled placements unchanged. |
| nx_create_reference_set | Native-tested | tested | real_NX_v2606_scoped | Native SOLIDS reference set containing one block body; saved and consumed by three assembly occurrences. |
| nx_curve_analysis | Native-tested | tested | real_NX_v2606_scoped | Native derivative evaluation on owned line/spline curves; singular handling unit-tested; sampling is not a global extrema certificate. |
| nx_delete_explosion | Native-tested | tested | real_NX_v2606_scoped | Native unassociated explosion deletion followed by empty explosion inventory; in-use rejection and stale invalidation covered locally. |
| nx_delete_feature | Native-tested | tested | real_NX_v2606_scoped | Native extrusion deletion after Save As with a reacquired feature ID; resulting body count zero. Dependency cascades beyond this fixture are not independently tested. |
| nx_display_info | Native-tested | tested | real_NX_v2606_public_MCP | Body/face and nested occurrence color, transparency and explicit blank state |
| nx_edit_assembly_constraint | Native-tested | tested | real_NX_v2606_scoped | Native suppression toggle and distance 5-&gt;12 edit; actual component separation verified after rebuilding solve network. |
| nx_edit_component_pattern | Native-tested | tested | real_NX_v2606_scoped | Native rectangular 4x3 and circular 5-instance edits; expression and instance readback. |
| nx_edit_explosion | Native-tested | tested | real_NX_v2606_scoped | Native nested assembly explosion: absolute rotated parent/child poses, reset, repeat assignment, model/drawing association, persistence; ordinary assembled placements unchanged. |
| nx_edit_explosion_trace | Native-tested | tested | real_NX_v2606_scoped | Native managed edge-anchored trace endpoint percentages and offsets edited in a two-component service assembly; rendered and included in drafting view. |
| nx_explosion_info | Native-tested | tested | real_NX_v2606_scoped | Native exploded and assembled occurrence poses and typed associated view references, including nested assembly. |
| nx_explosion_trace | Native-tested | tested | real_NX_v2606_scoped | Native traceline with persistent component/edge handles; exact endpoints preserved after save/reopen and updated by MCP placement changes. Manual edits require MCP refresh. |
| nx_export_explosion_animation | Native-tested | tested | real_NX_v2606_scoped | Three native frames with fixed camera, pose interpolation and restored state; fully framed visual review. Failure cleanup unit-tested. |
| nx_export_flat_pattern | Native-tested | tested | real_NX_v2606_scoped | Native DXF and Trumpf GEO export; staged file publication, checksums. DXF entity geometry inspected. |
| nx_face_analysis | Native-tested | tested | real_NX_v2606_scoped | Native sampled plane normal/curvature and signed draft; trimmed-domain filtering. Not global draft certification. |
| nx_find_geometry | Native-tested | tested | real_NX_v2606_scoped_and_local_boundary_tests | Native trimmed BREP point-to-face/edge distance; selector queries, principal plane filter and radius filter. Highest/lowest retain conservative center ordering. |
| nx_finish_preview | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_fit_view | Native-tested | tested | real_NX_v2606_scoped | Native fit on a displayed solid fixture in graphical NX; framing quality remains view-dependent. |
| nx_flat_pattern_orientation_edges | Native-tested | tested | real_NX_v2606_scoped | Native planar formed-web straight boundary discovery; returned edge created a Flat Pattern on first attempt. Geometric candidates only; not a kernel acceptance guarantee. |
| nx_geometry_anchor | Native-tested | tested | real_NX_v2606_scoped | Owned face persistent handle, owner-part identity and exact native resolution after save/reopen; no nearest-geometry fallback. |
| nx_get_bounding_box | Native-tested | tested | real_NX_v2606 | Part and two-level assembly; conservative and exact with axis-aligned WCS |
| nx_highlight_collisions | Native-tested | tested | real_NX_v2606_public_MCP | Native highlights on two intersecting nested body occurrences; clear pair not highlighted; inline viewport verified |
| nx_highlight_objects | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_inspection_report | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_list_annotations | Native-tested | tested | real_NX_v2606_scoped | Native BOM, balloon and managed sheet-metal PMI enumeration and text. |
| nx_list_assembly_constraints | Native-tested | tested | real_NX_v2606_scoped | Native typed constraint references, geometry/occurrence references, expressions, suppression and solver statuses. |
| nx_list_bodies | Native-tested | tested | real_NX_v2606_scoped | Native work-part inventory reports one extruded body and zero after feature deletion. |
| nx_list_component_patterns | Native-tested | tested | real_NX_v2606_scoped | Native linear, two-direction rectangular and circular pattern metadata and actual occurrence transforms. |
| nx_list_components | Native-tested | tested | real_NX_v2606 | Native loaded and unloaded prototype inventory retains occurrence references, paths and transforms with explicit load_state. Unknown source paths are nullable; unloaded subassemblies can hide descendants. No implicit loading. |
| nx_list_datums | Native-tested | tested | real_NX_v2606_scoped | Native owned datum planes/axes and coordinate-system enumeration on template part. |
| nx_list_dimensions | Native-tested | tested | real_NX_v2606_scoped | Native computed size and retention diagnostics; occurrence-edge dimension follows extrusion resize and explicitly rebinds after replacement. |
| nx_list_drawings | Native-tested | tested | real_NX_v2606_scoped | Native A3 sheet/view enumeration, dimensions, scale and active state. |
| nx_list_explosions | Native-tested | tested | real_NX_v2606_scoped | Native nested assembly explosion: absolute rotated parent/child poses, reset, repeat assignment, model/drawing association, persistence; ordinary assembled placements unchanged. |
| nx_list_expressions | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_list_features | Native-tested | tested | real_NX_v2606_scoped | Native sketch/extrusion inventory and reacquisition of a renamed feature following Save As. |
| nx_list_reference_sets | Native-tested | tested | real_NX_v2606_scoped | Native body-only custom set enumeration and exact member count. |
| nx_list_sections | Native-tested | tested | real_NX_v2606_public_MCP | Native plane enumeration; active view state and saved flags preserved |
| nx_list_sketches | Native-tested | tested | real_NX_v2606_scoped | Native two-sketch work-part inventory with typed references. |
| nx_list_topology | Native-tested | tested | real_NX_v2606 | Face and edge enumeration; face references used in actual distance query Native face-restricted boundary enumeration and bidirectional adjacency used for flat-pattern orientation. |
| nx_mass_properties | Native-tested | tested | real_NX_v2606_scoped | Native solid mass, volume, center of gravity and centroidal inertia; 2700kg/m3 test cube matches analytic results. Nested rotated/translated two-body assembly: mass0.0054kg and CoG[0.005,0.035,0.035]m verified. |
| nx_mate_component | Native-tested | tested | real_NX_v2606_scoped | Native touch mate at zero clearance and offset mate at7mm verified by measured separation. Other mate types have narrower validation. |
| nx_measure_angle | Native-tested | tested | real_NX_v2606_scoped | Native perpendicular sketch lines measure 90 degrees using typed references. Straight-edge and planar-face paths use native geometry; broader pair variants are not separately kernel-tested. |
| nx_measure_distance | Native-tested | tested | real_NX_v2606 | Body/body, face/face, nested component/body occurrences; closest points and units |
| nx_measure_volume | Native-tested | tested | real_NX_v2606 | Part and nested assembly sum, returned in mm^3; no union/mass claim Inch-part 0.5 cubic inch volume independently checked as 8193.532 mm3 using explicit native AnalysisUnit. |
| nx_model_health | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_model_summary | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_native_component_pattern | Native-tested | tested | real_NX_v2606_scoped_and_local_boundary_tests | NX 2606 native associative linear pattern: 16 total occurrences of 14 mm seed at 16.5 mm pitch span 261.5 mm. |
| nx_operation_status | Contract/sidecar-tested | tested | local_contract_test | Durable committed/failed/unknown receipt tests; no crash reconstruction claimed |
| nx_pattern_components | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_preview_change | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_read_result | Contract/sidecar-tested | tested | local_contract_tests | Immutable bounded bridge snapshots, JSON-pointer pagination, result cardinality and committed-outcome preservation on storage failure; 2032-object response fixture. |
| nx_recognize_holes | Native-tested | tested | real_NX_v2606_scoped_and_local_boundary_tests | Annular solid: inner cylinder identified as bore, outer cylinder excluded; axis/radius/full circumference read-back. Coaxial grouping and partial-face reporting covered locally; no manufacturing feature inference. |
| nx_rename_object | Native-tested | tested | real_NX_v2606_scoped | Native extrusion rename resolved again by returned name after Save As. Other object kinds are not independently covered by this fixture. |
| nx_render_view | Native-tested | tested | real_NX_v2606_scoped | Native Studio image capture, exact 800x600 and 640x480 PNGs; preset2/custom RGB tested; native viewport image visually reviewed. |
| nx_reposition_component | Native-tested | tested | real_NX_v2606_scoped | Native immediate-child 20-unit X translation and 90-degree Z rotation; identical operation-ID replay does not apply translation twice. |
| nx_resolve_geometry | Native-tested | tested | real_NX_v2606_scoped_and_local_boundary_tests | Exact point-to-face query re-evaluated after extrusion edit and save/close/reopen; ties rejected. Geometric rule, not immutable topology identity. |
| nx_resolve_geometry_anchor | Native-tested | tested | real_NX_v2606_scoped | Owned face survives save/reopen and rollback in a public USB connector STEP fixture; stale and wrong-owner rejection tested locally. |
| nx_restore_display | Native-tested | tested | real_NX_v2606_public_MCP | Reverse-order restore; invalid order rejected before mutation; face IDs retained across appearance and camera changes |
| nx_restore_presentation | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_rollback | Native-tested | tested | real_NX_v2606 | Explicit checkpoint rollback; stale references rejected afterward |
| nx_save_presentation | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_screenshot | Native-tested | tested | real_NX_v2606_interactive | Native viewport PNG, white/transparent backgrounds, shaded/shaded-with-edges; requested dimensions advisory; actual device resolution returned |
| nx_section_control | Native-tested | tested | real_NX_v2606_public_MCP | Enable, disable and delete native dynamic sections without modifying solids |
| nx_section_view | Native-tested | tested | real_NX_v2606_public_MCP | Principal and arbitrary single-plane clips on solids and assemblies; native cap images; geometry bounds and volume unchanged |
| nx_set_camera | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_set_component_reference_set | Native-tested | tested | real_NX_v2606_scoped | Native direct-child assignment to three occurrences, unchanged translations, persisted drawing excludes prototype datums. Nested children require activating owner. |
| nx_set_component_transform | Native-tested | tested | real_NX_v2606 | Absolute immediate-child placement; repeated identical pose |
| nx_set_datum_visibility | Native-tested | tested | real_NX_v2606_scoped | Native blank/unblank snapshot restoration, and assembly-owned datum suppression confirmed by exported drawing PDF visual review. |
| nx_set_display | Native-tested | tested | real_NX_v2606_public_MCP | Named color and transparency; face attribute restoration; nested occurrence override leaves shared prototypes unchanged |
| nx_set_sheet_metal_defaults | Native-tested | tested | real_NX_v2606_scoped | Value-mode thickness/radius/neutral factor and numeric read-back. Material/tool tables and custom bend tables remain experimental. |
| nx_set_view | Native-tested | tested | real_NX_v2606_scoped | Native Top, Back and Isometric selection on a solid fixture. Other canned views follow the same API but are not individually verified. |
| nx_set_visibility | Native-tested | tested | real_NX_v2606_public_MCP | Show/hide, nested isolation and restoration of previously hidden components |
| nx_sew | Native-tested | tested | real_NX_v2606_scoped | Native adjacent planar-sheet sewing; incomplete-sew and solid-fallback rejection covered by unit tests. |
| nx_sheet_metal_annotation | Native-tested | tested | real_NX_v2606_scoped | Native body/bend PMI with measured snapshot text and explicit refresh; no automatic numeric text update claim. |
| nx_sheet_metal_context | Native-tested | tested | real_NX_v2606_scoped | Modern graphical UG_APP_SBSM context; no legacy application switch or model undo mark. |
| nx_sheet_metal_defaults | Native-tested | tested | real_NX_v2606_scoped | Numeric defaults and bend-definition read-back; unsafe native material catalog enumeration intentionally not called. |
| nx_sheet_metal_feature | Native-tested | tested | real_NX_v2606_scoped | 34 native creation fixtures on NX v2606; flange builder edit verified. Individual options and other edit combinations remain experimental. Two-bend partial-width channel with Square/Round reliefs and independently calculated developed DXF dimensions; adjacent mitered flanges and flat export. |
| nx_sheet_metal_info | Native-tested | tested | real_NX_v2606_scoped | Native body recognition, thickness, inner bend faces, angle/radius/neutral factor read-back. |
| nx_sheet_metal_schema | Native-tested | tested | real_NX_v2606_scoped | Strict schemas for 34 native operation families; per-operation examples and validation scopes. |
| nx_show_explosion | Native-tested | tested | real_NX_v2606_scoped | Native nested assembly explosion: absolute rotated parent/child poses, reset, repeat assignment, model/drawing association, persistence; ordinary assembled placements unchanged. |
| nx_spline | Native-tested | tested | real_NX_v2606_scoped | Native associative 3D interpolation spline creation/edit and degree-2 control-pole curves; periodic variants unverified. |
| nx_status | Native-tested | tested | real_NX_v2606_scoped | Live graphical NX 2606 connection, version and UI/main-thread status; restored saved session. |
| nx_surface_continuity | Native-tested | tested | real_NX_v2606_scoped | Bidirectional sampled UF geometry: matching planes pass G0/G1/G2, separated planes fail, tangent plane/quadratic join fails G2. Not a global certificate. |
| nx_surface_mesh | Native-tested | tested | real_NX_v2606_scoped | Native planar and quadratic Through Curve Mesh fixtures from two primary and two cross sections. |
| nx_thicken | Native-tested | tested | real_NX_v2606_scoped | Native 2 mm sheet thickening with independently checked 400 mm^3 solid volume. |
| nx_trim_sheet | Native-tested | tested | real_NX_v2606_scoped | Native line-boundary half-sheet trim using section and region point. |
| nx_ui_control | Native-tested | tested | real_NX_v2606_interactive | UI-thread identity, visible model mutations, manual pause rejection and resume; native panel |
| nx_undo | Native-tested | tested | real_NX_v2606 | Undo after read-only inspection; save boundary explicitly inspected |
| nx_update_assembly_documentation | Native-tested | tested | real_NX_v2606_scoped | Two-component assembly: extrusion resize and prototype replacement, fixed mates, clearance, explosion traces, BOM and drawing regeneration. Retained dimensions are reported and explicitly rebound. |
| nx_view_info | Native-tested | tested | real_NX_v2606_interactive | Interactive display-part camera axes, origin and scale |
| nx_wall_thickness | Native-tested | tested | real_NX_v2606_scoped | Native inward-normal ray thickness: nine 5 mm plate samples. Not rolling-ball or global-minimum thickness. |
| nx_workspace_info | Contract/sidecar-tested | tested | live_sidecar_and_contract_tests | NX host workspace root and path conventions; no NX geometry calls. |

## Explicitly unavailable capabilities

These are capability names, separate from the tool counts above.

- batch_model_viewport_image
- union_of_all_pairwise_interference_volumes

## Manifest limitations

- No general certification
- Native save expires undo marks
- Checkpoint recovery does not survive NX process restart
- Exact bounds require axis-aligned WCS
- Assembly import can conflict with loaded STEP prototype names
- Modified-object tracking is explicit-only; null means not comprehensive
- Batch structural preflight is not a geometric dry run
- Cooperative cancellation happens between operations only
- Absolute placement currently supports immediate children
- MCP desktop clients must refresh tool schemas after deployment
- CLIFF vendor STEP conversion still returns no bodies with NX v2606 translators; logs and rolled-back new-part outcome are returned. No vendor-geometry repair is claimed.
- Adam Tech vendor geometry has two self-intersecting faces (875315); health diagnostics resolve faces and error text. Native healing failed on a disposable copy and was rolled back.
- Drawing centerline visibility is inspectable but not writable: the tested native setter did not persist.

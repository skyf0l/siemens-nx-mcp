# Capability evidence matrix

Generated from the catalogue (`nx_mcp.core.catalogue`) with the acceptance evidence in `src/nx_mcp/evidence/`, and for the remaining legacy tools from `src/nx_mcp/capability_manifest.json` plus `src/nx_mcp/capability_fragments/` (`nx_mcp.manifest.load_manifest()`); do not edit this table by hand.
Run `python scripts/generate_capability_matrix.py` to regenerate, or add `--check` to detect drift.

Manifest revision: **2606-manufacturing-r1**. NX: **v2606**. Bridge protocol: **1**.
Canonical SHA-256 (manifest and catalogue evidence): `a9f395d9a5e4abe05e2379de54a3c56b30ac0a75e906aba0cf869c44eb40ec6d`.

These labels report recorded evidence, not certification or independent verification of its claims. A catalogue tool is Native-tested when the acceptance runner recorded machine-checked scenario evidence for an NX build (listed per variant); without it the tool is Experimental, hidden from discovery and refused in production. A legacy tool is Native-tested with manifest status `tested` and an evidence type beginning `real_NX_`; only the stated scope and NX version are covered. Contract/sidecar-tested does not establish native CAD correctness. Experimental includes untested entries and tested entries without a recognized evidence type. Unavailable capabilities are explicitly recorded by the manifest; absence from this matrix is not proof of availability or unavailability.

| Tool classification | Count |
| --- | ---: |
| Native-tested | 154 |
| Contract/sidecar-tested | 4 |
| Experimental | 1 |
| Unavailable | 0 |

## Catalogue tools

| Tool | Classification | Domain | Effect | Acceptance evidence |
| --- | --- | --- | --- | --- |
| nx_activate_application | Native-tested | session | session | v2406: tested; 19 checks in acceptance/sheet_metal/activate_application.json |
| nx_activate_drawing_sheet | Native-tested | drawing | session | v2406: tested; 15 checks in acceptance/drawing/activate_drawing_sheet.json |
| nx_activate_part | Native-tested | parts_files | session | v2406: tested; 24 checks in acceptance/parts_files/activate_part.json |
| nx_analyze_curve | Native-tested | inspection | read | v2406: tested; 44 checks in acceptance/inspection/analysis.json |
| nx_analyze_faces | Native-tested | inspection | read | v2406: tested; 44 checks in acceptance/inspection/analysis.json |
| nx_capture_view | Native-tested | display | view | v2406: tested; 45 checks in acceptance/display/capture_view.json |
| nx_check_continuity | Native-tested | inspection | read | v2406: tested; 44 checks in acceptance/inspection/analysis.json |
| nx_check_interference | Native-tested | inspection | read | v2406: tested; 54 checks in acceptance/inspection/interference.json |
| nx_check_model | Native-tested | inspection | read | v2406: tested; 37 checks in acceptance/inspection/health.json |
| nx_check_sketch | Native-tested | sketch | read | v2406: tested; 28 checks in acceptance/sketch/check_sketch.json |
| nx_close_part | Native-tested | parts_files | session | v2406: tested; 18 checks in acceptance/parts_files/close_part.json |
| nx_create_anchor | Native-tested | inspection | read | v2406: tested; 51 checks in acceptance/inspection/geometry.json |
| nx_create_assembly_constraint | Native-tested | assembly | model | v2406: all variants; 82 checks in acceptance/assembly/constraints.json |
| nx_create_balloons | Native-tested | drawing | model | v2406: tested; 36 checks in acceptance/drawing/create_balloons.json |
| nx_create_blend | Native-tested | modeling | model | v2406: tested; 16 checks in acceptance/modeling/blend.json |
| nx_create_boolean | Native-tested | modeling | model | v2406: all variants; 17 checks in acceptance/modeling/boolean.json |
| nx_create_bridge_surface | Native-tested | freeform | model | v2406: all variants; 21 checks in acceptance/freeform/bridge_surface.json |
| nx_create_centerline | Native-tested | drawing | model | v2406: all variants; 23 checks in acceptance/drawing/create_centerline.json |
| nx_create_chamfer | Native-tested | modeling | model | v2406: all variants; 12 checks in acceptance/modeling/chamfer.json |
| nx_create_component | Native-tested | assembly | model | v2406: tested; 108 checks in acceptance/assembly/components.json |
| nx_create_component_pattern | Native-tested | assembly | model | v2406: tested; 74 checks in acceptance/assembly/patterns.json |
| nx_create_datum_axis | Native-tested | modeling | model | v2406: all variants; 14 checks in acceptance/modeling/datum_axis.json |
| nx_create_datum_csys | Native-tested | modeling | model | v2406: tested; 13 checks in acceptance/modeling/datum_csys.json |
| nx_create_datum_plane | Native-tested | modeling | model | v2406: all variants; 22 checks in acceptance/modeling/datum_plane.json |
| nx_create_datum_point | Native-tested | modeling | model | v2406: tested; 6 checks in acceptance/modeling/datum_point.json |
| nx_create_directory | Native-tested | parts_files | file | v2406: tested; 18 checks in acceptance/parts_files/create_directory.json |
| nx_create_draft | Native-tested | modeling | model | v2406: tested; 8 checks in acceptance/modeling/draft.json |
| nx_create_drawing_dimension | Native-tested | drawing | model | v2406: all variants; 23 checks in acceptance/drawing/inch_units.json |
| nx_create_drawing_sheet | Native-tested | drawing | model | v2406: tested; 23 checks in acceptance/drawing/inch_units.json |
| nx_create_drawing_symbol | Native-tested | drawing | model | v2406: all variants; 19 checks in acceptance/drawing/create_drawing_symbol.json |
| nx_create_drawing_table | Native-tested | drawing | model | v2406: all variants; 23 checks in acceptance/drawing/inch_units.json |
| nx_create_drawing_view | Native-tested | drawing | model | v2406: all variants; 23 checks in acceptance/drawing/inch_units.json |
| nx_create_explosion | Native-tested | assembly | model | v2406: tested; 93 checks in acceptance/assembly/explosions.json |
| nx_create_explosion_trace | Native-tested | assembly | model | v2406: tested; 93 checks in acceptance/assembly/explosions.json |
| nx_create_extrude | Native-tested | modeling | model | v2406: tested; 36 checks in acceptance/modeling/extrude.json |
| nx_create_gdt | Native-tested | drawing | model | v2406: fcf (untested: datum_feature); 21 checks in acceptance/drawing/create_gdt.json |
| nx_create_hole | Native-tested | modeling | model | v2406: all variants; 26 checks in acceptance/modeling/hole.json |
| nx_create_loft | Native-tested | modeling | model | v2406: tested; 13 checks in acceptance/modeling/loft.json |
| nx_create_mesh_surface | Native-tested | freeform | model | v2406: tested; 18 checks in acceptance/freeform/mesh_surface.json |
| nx_create_mirror | Native-tested | modeling | model | v2406: tested; 16 checks in acceptance/modeling/mirror.json |
| nx_create_note | Native-tested | drawing | model | v2406: tested; 23 checks in acceptance/drawing/inch_units.json |
| nx_create_part | Native-tested | parts_files | file | v2406: tested; 33 checks in acceptance/parts_files/create_part.json |
| nx_create_pattern | Native-tested | modeling | model | v2406: tested; 13 checks in acceptance/modeling/pattern.json |
| nx_create_reference_set | Native-tested | assembly | model | v2406: tested; 108 checks in acceptance/assembly/components.json |
| nx_create_revolve | Native-tested | modeling | model | v2406: tested; 17 checks in acceptance/modeling/revolve.json |
| nx_create_scale | Native-tested | modeling | model | v2406: tested; 12 checks in acceptance/modeling/scale.json |
| nx_create_section_view | Native-tested | display | view | v2406: tested; 40 checks in acceptance/display/sections.json |
| nx_create_sew | Native-tested | freeform | model | v2406: tested; 23 checks in acceptance/freeform/sew_thicken.json |
| nx_create_sheet_metal_callout | Experimental | sheet_metal | model | no acceptance evidence: hidden and refused in production |
| nx_create_sheet_metal_feature | Native-tested | sheet_metal | model | v2406: bend, break_corner, contour_flange, dimple, drawn_cutout, flange, flat_pattern, hem, jog, normal_cutout, rebend, resize_bend_angle, resize_bend_radius, resize_neutral_factor, tab, unbend (untested: louver); 14 checks in acceptance/sheet_metal/inch.json |
| nx_create_shell | Native-tested | modeling | model | v2406: tested; 9 checks in acceptance/modeling/shell.json |
| nx_create_sketch | Native-tested | sketch | model | v2406: all variants; 54 checks in acceptance/sketch/create_sketch.json |
| nx_create_sketch_constraints | Native-tested | sketch | model | v2406: all variants; 44 checks in acceptance/sketch/sketch_constraints.json |
| nx_create_sketch_copy | Native-tested | sketch | model | v2406: all variants; 35 checks in acceptance/sketch/sketch_copy.json |
| nx_create_sketch_corner | Native-tested | sketch | model | v2406: all variants; 25 checks in acceptance/sketch/sketch_corner.json |
| nx_create_sketch_curves | Native-tested | sketch | model | v2406: all variants; 52 checks in acceptance/sketch/sketch_curves.json |
| nx_create_sketch_dimensions | Native-tested | sketch | model | v2406: all variants; 39 checks in acceptance/sketch/sketch_dimensions.json |
| nx_create_sketch_projection | Native-tested | sketch | model | v2406: all variants; 26 checks in acceptance/sketch/sketch_projection.json |
| nx_create_spline | Native-tested | freeform | model | v2406: all variants; 25 checks in acceptance/freeform/spline.json |
| nx_create_sweep | Native-tested | modeling | model | v2406: tested; 12 checks in acceptance/modeling/sweep.json |
| nx_create_thicken | Native-tested | freeform | model | v2406: tested; 23 checks in acceptance/freeform/sew_thicken.json |
| nx_create_thread | Native-tested | modeling | model | v2406: tested; 19 checks in acceptance/modeling/thread.json |
| nx_create_transform | Native-tested | modeling | model | v2406: tested; 20 checks in acceptance/modeling/transform.json |
| nx_create_trim_body | Native-tested | modeling | model | v2406: all variants; 16 checks in acceptance/modeling/trim_body.json |
| nx_create_trim_sheet | Native-tested | freeform | model | v2406: tested; 11 checks in acceptance/freeform/trim_sheet.json |
| nx_download_file | Native-tested | parts_files | read | v2406: all variants; 27 checks in acceptance/parts_files/download_file.json |
| nx_edit_annotation | Native-tested | drawing | model | v2406: tested; 22 checks in acceptance/drawing/edit_annotation.json |
| nx_edit_assembly_constraint | Native-tested | assembly | model | v2406: tested; 82 checks in acceptance/assembly/constraints.json |
| nx_edit_component | Native-tested | assembly | model | v2406: tested; 108 checks in acceptance/assembly/components.json |
| nx_edit_component_pattern | Native-tested | assembly | model | v2406: tested; 74 checks in acceptance/assembly/patterns.json |
| nx_edit_dimension | Native-tested | drawing | model | v2406: tested; 30 checks in acceptance/drawing/edit_dimension.json |
| nx_edit_drawing_table | Native-tested | drawing | model | v2406: all variants; 18 checks in acceptance/drawing/edit_drawing_table.json |
| nx_edit_drawing_view | Native-tested | drawing | model | v2406: tested; 27 checks in acceptance/drawing/edit_drawing_view.json |
| nx_edit_explosion | Native-tested | assembly | model | v2406: tested; 93 checks in acceptance/assembly/explosions.json |
| nx_edit_explosion_trace | Native-tested | assembly | model | v2406: tested; 93 checks in acceptance/assembly/explosions.json |
| nx_edit_faces | Native-tested | modeling | model | v2406: all variants; 13 checks in acceptance/modeling/edit_faces.json |
| nx_edit_feature | Native-tested | modeling | model | v2406: tested; 16 checks in acceptance/modeling/edit_feature.json |
| nx_edit_section_view | Native-tested | display | view | v2406: tested; 40 checks in acceptance/display/sections.json |
| nx_edit_sketch_curves | Native-tested | sketch | model | v2406: all variants; 32 checks in acceptance/sketch/edit_sketch_curves.json |
| nx_export_assembly | Native-tested | parts_files | file | v2406: all variants; 42 checks in acceptance/parts_files/export_assembly.json |
| nx_export_drawing | Native-tested | drawing | file | v2406: tested; 29 checks in acceptance/drawing/export_drawing.json |
| nx_export_explosion_animation | Native-tested | assembly | file | v2406: tested; 93 checks in acceptance/assembly/explosions.json |
| nx_export_flat_pattern | Native-tested | sheet_metal | file | v2406: tested; 14 checks in acceptance/sheet_metal/inch.json |
| nx_export_inspection_report | Native-tested | inspection | file | v2406: tested; 54 checks in acceptance/inspection/interference.json |
| nx_export_model | Native-tested | parts_files | file | v2406: all variants; 35 checks in acceptance/parts_files/export_model.json |
| nx_export_profile | Native-tested | parts_files | file | v2406: tested; 38 checks in acceptance/parts_files/export_profile.json |
| nx_find_geometry | Native-tested | inspection | read | v2406: tested; 51 checks in acceptance/inspection/geometry.json |
| nx_find_holes | Native-tested | inspection | read | v2406: tested; 51 checks in acceptance/inspection/geometry.json |
| nx_finish_sketch | Native-tested | sketch | model | v2406: tested; 20 checks in acceptance/sketch/finish_sketch.json |
| nx_get_appearance | Native-tested | display | read | v2406: tested; 53 checks in acceptance/display/appearance.json |
| nx_get_camera | Native-tested | display | read | v2406: tested; 48 checks in acceptance/display/camera.json |
| nx_get_dimension | Native-tested | drawing | read | v2406: tested; 13 checks in acceptance/drawing/get_dimension.json |
| nx_get_drawing_table | Native-tested | drawing | read | v2406: tested; 11 checks in acceptance/drawing/get_drawing_table.json |
| nx_get_drawing_view | Native-tested | drawing | read | v2406: tested; 13 checks in acceptance/drawing/get_drawing_view.json |
| nx_get_explosion | Native-tested | assembly | read | v2406: tested; 93 checks in acceptance/assembly/explosions.json |
| nx_get_feature | Native-tested | modeling | read | v2406: tested; 14 checks in acceptance/modeling/get_feature.json |
| nx_get_material | Native-tested | modeling | read | v2406: tested; 14 checks in acceptance/modeling/material.json |
| nx_get_model_summary | Native-tested | inspection | read | v2406: tested; 37 checks in acceptance/inspection/health.json |
| nx_get_sheet_metal | Native-tested | sheet_metal | read | v2406: tested; 14 checks in acceptance/sheet_metal/inch.json |
| nx_get_sheet_metal_defaults | Native-tested | sheet_metal | read | v2406: tested; 30 checks in acceptance/sheet_metal/defaults.json |
| nx_get_sketch | Native-tested | sketch | read | v2406: tested; 34 checks in acceptance/sketch/get_sketch.json |
| nx_import_model | Native-tested | parts_files | model | v2406: all variants; 37 checks in acceptance/parts_files/import_model.json |
| nx_list_components | Native-tested | assembly | read | v2406: tested; 108 checks in acceptance/assembly/components.json |
| nx_list_files | Native-tested | parts_files | read | v2406: tested; 20 checks in acceptance/parts_files/list_files.json |
| nx_list_objects | Native-tested | inspection | read | v2406: tested; 20 checks in acceptance/inspection/list_objects.json |
| nx_list_parts | Native-tested | parts_files | read | v2406: tested; 17 checks in acceptance/parts_files/list_parts.json |
| nx_list_thread_sizes | Native-tested | modeling | read | v2406: tested; 19 checks in acceptance/modeling/thread.json |
| nx_measure_angle | Native-tested | inspection | read | v2406: tested; 51 checks in acceptance/inspection/measure.json |
| nx_measure_bounds | Native-tested | inspection | read | v2406: tested; 51 checks in acceptance/inspection/measure.json |
| nx_measure_distance | Native-tested | inspection | read | v2406: tested; 51 checks in acceptance/inspection/measure.json |
| nx_measure_mass | Native-tested | inspection | read | v2406: tested; 51 checks in acceptance/inspection/measure.json |
| nx_measure_wall_thickness | Native-tested | inspection | read | v2406: tested; 51 checks in acceptance/inspection/measure.json |
| nx_open_part | Native-tested | parts_files | session | v2406: tested; 28 checks in acceptance/parts_files/open_part.json |
| nx_resolve_reference | Native-tested | inspection | read | v2406: tested; 51 checks in acceptance/inspection/geometry.json |
| nx_restore_display | Native-tested | display | view | v2406: tested; 39 checks in acceptance/display/visibility.json |
| nx_save_part | Native-tested | parts_files | file | v2406: tested; 30 checks in acceptance/parts_files/save_part.json |
| nx_save_presentation | Native-tested | display | file | v2406: tested; 28 checks in acceptance/display/presentation.json |
| nx_set_appearance | Native-tested | display | view | v2406: tested; 53 checks in acceptance/display/appearance.json |
| nx_set_camera | Native-tested | display | view | v2406: tested; 48 checks in acceptance/display/camera.json |
| nx_set_component_pose | Native-tested | assembly | model | v2406: all variants; 108 checks in acceptance/assembly/components.json |
| nx_set_control | Native-tested | session | session | v2406: all variants; 19 checks in acceptance/session/set_control.json |
| nx_set_current_feature | Native-tested | modeling | model | v2406: tested; 10 checks in acceptance/modeling/current_feature.json |
| nx_set_explosion_display | Native-tested | assembly | model | v2406: tested; 93 checks in acceptance/assembly/explosions.json |
| nx_set_expression | Native-tested | modeling | model | v2406: tested; 24 checks in acceptance/modeling/expression.json |
| nx_set_feature_order | Native-tested | modeling | model | v2406: tested; 12 checks in acceptance/modeling/feature_order.json |
| nx_set_feature_suppression | Native-tested | modeling | model | v2406: tested; 16 checks in acceptance/modeling/feature_suppression.json |
| nx_set_highlight | Native-tested | display | view | v2406: tested; 10 checks in acceptance/display/highlight.json |
| nx_set_material | Native-tested | modeling | model | v2406: tested; 14 checks in acceptance/modeling/material.json |
| nx_set_sheet_metal_defaults | Native-tested | sheet_metal | model | v2406: tested; 14 checks in acceptance/sheet_metal/inch.json |
| nx_set_visibility | Native-tested | display | view | v2406: tested; 39 checks in acceptance/display/visibility.json |
| nx_update_annotations | Native-tested | drawing | model | v2406: tested; 6 checks in acceptance/drawing/update_annotations.json |
| nx_update_assembly | Native-tested | assembly | model | v2406: tested; 19 checks in acceptance/assembly/update_assembly.json |
| nx_update_model | Native-tested | modeling | model | v2406: tested; 7 checks in acceptance/modeling/update_model.json |
| nx_upload_file | Native-tested | parts_files | file | v2406: tested; 28 checks in acceptance/parts_files/upload_file.json |

## Legacy tools

| Tool | Classification | Manifest status | Evidence type | Tested scope / caveat |
| --- | --- | --- | --- | --- |
| nx_batch | Native-tested | tested | real_NX_v2606 | Two-step partial failure fully rolled back; serial execution on journal thread |
| nx_cancel_operation | Contract/sidecar-tested | tested | local_contract_tests | Sidecar contract accepts cancellation only for running batches and creates the cancellation marker; batch boundary seam verifies rollback. Live non-running request rejected. No mid-NXOpen interruption claim. |
| nx_capabilities | Native-tested | tested | real_NX_v2606_scoped | Live exact-tool manifest filtering, NX version and API-detection response. API presence is not a geometry correctness certificate. |
| nx_checkpoint | Native-tested | tested | real_NX_v2606 | In-session model checkpoint and available-state inspection |
| nx_checkpoint_state | Native-tested | tested | real_NX_v2606 | Checks actual NX mark availability, including save expiration |
| nx_delete_explosion | Native-tested | tested | real_NX_v2606_scoped | Native unassociated explosion deletion followed by empty explosion inventory; in-use rejection and stale invalidation covered locally. |
| nx_delete_feature | Native-tested | tested | real_NX_v2606_scoped | Native extrusion deletion after Save As with a reacquired feature ID; resulting body count zero. Dependency cascades beyond this fixture are not independently tested. |
| nx_finish_preview | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_list_annotations | Native-tested | tested | real_NX_v2606_scoped | Native BOM, balloon and managed sheet-metal PMI enumeration and text. |
| nx_list_bodies | Native-tested | tested | real_NX_v2606_scoped | Native work-part inventory reports one extruded body and zero after feature deletion. |
| nx_list_datums | Native-tested | tested | real_NX_v2606_scoped | Native owned datum planes/axes and coordinate-system enumeration on template part. |
| nx_list_dimensions | Native-tested | tested | real_NX_v2606_scoped | Native computed size and retention diagnostics; occurrence-edge dimension follows extrusion resize and explicitly rebinds after replacement. |
| nx_list_drawings | Native-tested | tested | real_NX_v2606_scoped | Native A3 sheet/view enumeration, dimensions, scale and active state. |
| nx_list_expressions | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_list_features | Native-tested | tested | real_NX_v2606_scoped | Native sketch/extrusion inventory and reacquisition of a renamed feature following Save As. |
| nx_list_sketches | Native-tested | tested | real_NX_v2606_scoped | Native two-sketch work-part inventory with typed references. |
| nx_list_topology | Native-tested | tested | real_NX_v2606 | Face and edge enumeration; face references used in actual distance query Native face-restricted boundary enumeration and bidirectional adjacency used for flat-pattern orientation. |
| nx_operation_status | Contract/sidecar-tested | tested | local_contract_test | Durable committed/failed/unknown receipt tests; no crash reconstruction claimed |
| nx_preview_change | Native-tested | tested | real_NX_v2606_and_local_stateful_seams | Scoped v2606 native authoring/review acceptance and local failure-path regressions; see docs/tools.md for supported operations and limits. |
| nx_read_result | Contract/sidecar-tested | tested | local_contract_tests | Immutable bounded bridge snapshots, JSON-pointer pagination, result cardinality and committed-outcome preservation on storage failure; 2032-object response fixture. |
| nx_rename_object | Native-tested | tested | real_NX_v2606_scoped | Native extrusion rename resolved again by returned name after Save As. Other object kinds are not independently covered by this fixture. |
| nx_rollback | Native-tested | tested | real_NX_v2606 | Explicit checkpoint rollback; stale references rejected afterward |
| nx_status | Native-tested | tested | real_NX_v2606_scoped | Live graphical NX 2606 connection, version and UI/main-thread status; restored saved session. |
| nx_undo | Native-tested | tested | real_NX_v2606 | Undo after read-only inspection; save boundary explicitly inspected |
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

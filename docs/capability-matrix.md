# Capability evidence matrix

Generated from the catalogue (`nx_mcp.core.catalogue`) with the acceptance evidence in `src/nx_mcp/evidence/`; do not edit this table by hand.
Run `python scripts/generate_capability_matrix.py` to regenerate, or add `--check` to detect drift.

Canonical SHA-256 (catalogue evidence): `711554911741ce60b1b490c08d3f601216bb98edee0d3bb29ea70d4e1db43058`.

These labels report recorded evidence, not certification or independent verification of its claims. A catalogue tool is Native-tested when the acceptance runner recorded machine-checked scenario evidence for an NX build (listed per variant); only that scope and build are covered. Without it the tool is Experimental, hidden from discovery and refused in production.

| Tool classification | Count |
| --- | ---: |
| Native-tested | 267 |
| Experimental | 2 |

## Catalogue tools

| Tool | Classification | Domain | Effect | Acceptance evidence |
| --- | --- | --- | --- | --- |
| nx_activate_application | Native-tested | session | session | v2406: tested; 21 checks in acceptance/sheet_metal/activate_application.json |
| nx_activate_drawing_sheet | Native-tested | drawing | session | v2406: tested; 19 checks in acceptance/drawing/out_of_date_views.json |
| nx_activate_part | Native-tested | parts_files | session | v2406: tested; 44 checks in acceptance/parts_files/parts_checks.json |
| nx_analyze_body_changes | Native-tested | inspection | read | v2406: tested; 22 checks in acceptance/inspection/body_compare.json |
| nx_analyze_curve | Native-tested | inspection | read | v2406: tested; 52 checks in acceptance/inspection/measure_details.json |
| nx_analyze_faces | Native-tested | inspection | read | v2406: tested; 52 checks in acceptance/inspection/measure_details.json |
| nx_analyze_topology_study | Native-tested | optimization | model | v2406: tested; 39 checks in acceptance/optimization/strength.json |
| nx_cancel_operation | Native-tested | session | session | v2406: tested; 62 checks in acceptance/session/status_operations.json |
| nx_capture_view | Native-tested | display | view | v2406: tested; 65 checks in acceptance/display/natural_args.json |
| nx_check_continuity | Native-tested | inspection | read | v2406: tested; 127 checks in acceptance/inspection/analysis.json |
| nx_check_drawing_changes | Native-tested | drawing | read | v2406: tested; 30 checks in acceptance/drawing/drawing_compare.json |
| nx_check_face_continuity | Native-tested | inspection | read | v2406: tested; 40 checks in acceptance/inspection/surface_quality.json |
| nx_check_interference | Native-tested | inspection | read | v2406: tested; 52 checks in acceptance/inspection/measure_details.json |
| nx_check_model | Native-tested | inspection | read | v2406: tested; 19 checks in acceptance/inspection/self_overlap_face_limit.json |
| nx_check_sketch | Native-tested | sketch | read | v2406: tested; 55 checks in acceptance/sketch/sketch_natural_args.json |
| nx_close_part | Native-tested | parts_files | session | v2406: tested; 58 checks in acceptance/parts_files/part_ownership.json |
| nx_create_anchor | Native-tested | inspection | read | v2406: tested; 91 checks in acceptance/inspection/holes_and_words.json |
| nx_create_arrangement | Native-tested | assembly | model | v2406: tested; 24 checks in acceptance/assembly/assembly_cut.json |
| nx_create_assembly_constraint | Native-tested | assembly | model | v2406: all variants; 82 checks in acceptance/assembly/constraints.json |
| nx_create_assembly_cut | Native-tested | assembly | model | v2406: tested; 24 checks in acceptance/assembly/assembly_cut.json |
| nx_create_balloons | Native-tested | drawing | model | v2406: tested; 39 checks in acceptance/drawing/create_balloons.json |
| nx_create_blend | Native-tested | modeling | model | v2406: tested; 8 checks in acceptance/modeling/tangent_chain.json |
| nx_create_blend_corner | Native-tested | modeling | model | v2406: tested; 16 checks in acceptance/modeling/blend_corner.json |
| nx_create_boolean | Native-tested | modeling | model | v2406: all variants; 8 checks in acceptance/modeling/unite_readback.json |
| nx_create_bounded_plane | Native-tested | freeform | model | v2406: tested; 29 checks in acceptance/freeform/bounded.json |
| nx_create_bridge_curve | Native-tested | freeform | model | v2406: tested; 85 checks in acceptance/freeform/curve_edit.json |
| nx_create_bridge_surface | Native-tested | freeform | model | v2406: all variants; 41 checks in acceptance/freeform/surface_checks.json |
| nx_create_centerline | Native-tested | drawing | model | v2406: all variants; 20 checks in acceptance/drawing/symbols_centerlines.json |
| nx_create_chamfer | Native-tested | modeling | model | v2406: all variants; 8 checks in acceptance/modeling/tangent_chain.json |
| nx_create_checkpoint | Native-tested | session | session | v2406: tested; 41 checks in acceptance/session/undo_survives_load.json |
| nx_create_clearance_set | Native-tested | assembly | model | v2406: tested; 66 checks in acceptance/assembly/clearance_sets.json |
| nx_create_combined_projection | Native-tested | freeform | model | v2406: tested; 14 checks in acceptance/freeform/curve_combined.json |
| nx_create_component | Native-tested | assembly | model | v2406: all variants; 42 checks in acceptance/assembly/new_component.json |
| nx_create_component_group | Native-tested | assembly | model | v2406: tested; 88 checks in acceptance/assembly/weight_management.json |
| nx_create_component_mirror | Native-tested | assembly | model | v2406: tested; 68 checks in acceptance/assembly/deformable.json |
| nx_create_component_pattern | Native-tested | assembly | model | v2406: tested; 88 checks in acceptance/assembly/patterns.json |
| nx_create_curve_on_surface | Native-tested | freeform | model | v2406: tested; 52 checks in acceptance/freeform/curve_surface.json |
| nx_create_datum_axis | Native-tested | modeling | model | v2406: all variants; 20 checks in acceptance/modeling/datum_axis_references.json |
| nx_create_datum_csys | Native-tested | modeling | model | v2406: tested; 31 checks in acceptance/modeling/results_reported.json |
| nx_create_datum_plane | Native-tested | modeling | model | v2406: all variants; 27 checks in acceptance/modeling/expression_drive.json |
| nx_create_datum_point | Native-tested | modeling | model | v2406: tested; 31 checks in acceptance/modeling/results_reported.json |
| nx_create_decal | Native-tested | display | model | v2406: tested; 62 checks in acceptance/display/visual_materials.json |
| nx_create_deformable_definition | Native-tested | assembly | model | v2406: tested; 68 checks in acceptance/assembly/deformable.json |
| nx_create_delete_body | Native-tested | modeling | model | v2406: tested; 35 checks in acceptance/modeling/repair.json |
| nx_create_directory | Native-tested | parts_files | file | v2406: tested; 44 checks in acceptance/parts_files/parts_checks.json |
| nx_create_draft | Native-tested | modeling | model | v2406: tested; 27 checks in acceptance/modeling/expression_drive.json |
| nx_create_draft_body | Native-tested | modeling | model | v2406: tested; 15 checks in acceptance/modeling/draft_body.json |
| nx_create_drawing_dimension | Native-tested | drawing | model | v2406: all variants; 56 checks in acceptance/drawing/view_edges_bounds.json |
| nx_create_drawing_sheet | Native-tested | drawing | model | v2406: tested; 23 checks in acceptance/drawing/inch_units.json |
| nx_create_drawing_symbol | Native-tested | drawing | model | v2406: all variants; 24 checks in acceptance/drawing/create_drawing_symbol.json |
| nx_create_drawing_table | Native-tested | drawing | model | v2406: all variants; 57 checks in acceptance/drawing/title_block.json |
| nx_create_drawing_view | Native-tested | drawing | model | v2406: all variants; 36 checks in acceptance/drawing/view_visibility.json |
| nx_create_emboss | Native-tested | modeling | model | v2406: all variants; 28 checks in acceptance/modeling/refusals_before_nx.json |
| nx_create_emboss_body | Native-tested | modeling | model | v2406: tested; 8 checks in acceptance/modeling/emboss_body.json |
| nx_create_explosion | Native-tested | assembly | model | v2406: tested; 161 checks in acceptance/assembly/explosions.json |
| nx_create_explosion_trace | Native-tested | assembly | model | v2406: tested; 161 checks in acceptance/assembly/explosions.json |
| nx_create_extract | Native-tested | assembly | model | v2406: all variants; 79 checks in acceptance/assembly/wave_links.json |
| nx_create_extrude | Native-tested | modeling | model | v2406: tested; 55 checks in acceptance/modeling/extrude.json |
| nx_create_face_blend | Native-tested | modeling | model | v2406: tested; 21 checks in acceptance/modeling/face_blend.json |
| nx_create_face_copy | Native-tested | modeling | model | v2406: all variants; 16 checks in acceptance/modeling/sync_faces.json |
| nx_create_face_label | Native-tested | modeling | model | v2406: all variants; 8 checks in acceptance/modeling/sync_labels.json |
| nx_create_face_paste | Native-tested | modeling | model | v2406: all variants; 16 checks in acceptance/modeling/sync_faces.json |
| nx_create_fill_hole | Native-tested | freeform | model | v2406: all variants; 25 checks in acceptance/freeform/fill_hole.json |
| nx_create_fit_curve | Native-tested | freeform | model | v2406: all variants; 7 checks in acceptance/freeform/curve_fit_ellipse.json |
| nx_create_gdt | Native-tested | drawing | model | v2406: fcf, view_datum_feature, view_datum_target, view_fcf (untested: datum_feature); 18 checks in acceptance/drawing/gdt_views_inch.json |
| nx_create_general_conic | Native-tested | freeform | model | v2406: all variants; 42 checks in acceptance/freeform/curve_law.json |
| nx_create_hatch | Native-tested | drawing | model | v2406: tested; 53 checks in acceptance/drawing/sheet_sketch_hatch.json |
| nx_create_helix | Native-tested | modeling | model | v2406: tested; 9 checks in acceptance/modeling/shape_checks.json |
| nx_create_hole | Native-tested | modeling | model | v2406: all variants; 48 checks in acceptance/modeling/thread_natural_args.json |
| nx_create_interpart_link | Native-tested | assembly | model | v2406: tested; 24 checks in acceptance/assembly/interpart_expressions.json |
| nx_create_intersection_curve | Native-tested | freeform | model | v2406: tested; 16 checks in acceptance/freeform/curve_intersection.json |
| nx_create_isocline_curves | Native-tested | freeform | model | v2406: all variants; 52 checks in acceptance/freeform/curve_surface.json |
| nx_create_isoparametric_curves | Native-tested | freeform | model | v2406: all variants; 52 checks in acceptance/freeform/curve_surface.json |
| nx_create_joined_curve | Native-tested | freeform | model | v2406: tested; 85 checks in acceptance/freeform/curve_edit.json |
| nx_create_law_curve | Native-tested | freeform | model | v2406: tested; 42 checks in acceptance/freeform/curve_law.json |
| nx_create_law_extension | Native-tested | freeform | model | v2406: all variants; 41 checks in acceptance/freeform/surface_checks.json |
| nx_create_loft | Native-tested | modeling | model | v2406: tested; 31 checks in acceptance/modeling/results_reported.json |
| nx_create_measurement | Native-tested | modeling | model | v2406: all variants; 4 checks in acceptance/modeling/measure_feature_refs.json |
| nx_create_mesh_surface | Native-tested | freeform | model | v2406: tested; 46 checks in acceptance/freeform/mesh_surface.json |
| nx_create_midsurface | Native-tested | freeform | model | v2406: all variants; 41 checks in acceptance/freeform/surface_checks.json |
| nx_create_mirror | Native-tested | modeling | model | v2406: all variants; 23 checks in acceptance/modeling/pattern_unite.json |
| nx_create_mirror_curve | Native-tested | freeform | model | v2406: tested; 85 checks in acceptance/freeform/curve_edit.json |
| nx_create_n_sided_surface | Native-tested | freeform | model | v2406: all variants; 41 checks in acceptance/freeform/surface_checks.json |
| nx_create_note | Native-tested | drawing | model | v2406: tested; 23 checks in acceptance/drawing/inch_units.json |
| nx_create_offset_curve | Native-tested | freeform | model | v2406: all variants; 35 checks in acceptance/freeform/curve_offset.json |
| nx_create_offset_emboss | Native-tested | modeling | model | v2406: tested; 28 checks in acceptance/modeling/refusals_before_nx.json |
| nx_create_offset_surface | Native-tested | freeform | model | v2406: tested; 41 checks in acceptance/freeform/surface_checks.json |
| nx_create_part | Native-tested | parts_files | file | v2406: tested; 44 checks in acceptance/parts_files/parts_checks.json |
| nx_create_part_family | Native-tested | parts_files | file | v2406: tested; 44 checks in acceptance/parts_files/parts_checks.json |
| nx_create_part_family_table | Native-tested | drawing | model | v2406: tested; 16 checks in acceptance/drawing/family_table.json |
| nx_create_patch_body | Native-tested | modeling | model | v2406: tested; 35 checks in acceptance/modeling/repair.json |
| nx_create_pattern | Native-tested | modeling | model | v2406: all variants; 23 checks in acceptance/modeling/pattern_unite.json |
| nx_create_primitive | Native-tested | modeling | model | v2406: all variants; 38 checks in acceptance/modeling/primitives.json |
| nx_create_projected_curve | Native-tested | freeform | model | v2406: all variants; 26 checks in acceptance/freeform/curve_project.json |
| nx_create_reference_set | Native-tested | assembly | model | v2406: tested; 146 checks in acceptance/assembly/components.json |
| nx_create_refit_face | Native-tested | freeform | model | v2406: tested; 11 checks in acceptance/freeform/refit.json |
| nx_create_revolve | Native-tested | modeling | model | v2406: tested; 21 checks in acceptance/modeling/revolve_profile.json |
| nx_create_rib | Native-tested | modeling | model | v2406: all variants; 9 checks in acceptance/modeling/shape_checks.json |
| nx_create_ruled | Native-tested | freeform | model | v2406: all variants; 41 checks in acceptance/freeform/surface_checks.json |
| nx_create_scale | Native-tested | modeling | model | v2406: tested; 16 checks in acceptance/modeling/scale_general.json |
| nx_create_section_curves | Native-tested | freeform | model | v2406: all variants; 35 checks in acceptance/freeform/curve_section.json |
| nx_create_section_view | Native-tested | display | view | v2406: tested; 24 checks in acceptance/display/sections_switch.json |
| nx_create_sew | Native-tested | freeform | model | v2406: tested; 47 checks in acceptance/freeform/sew_thicken.json |
| nx_create_sheet_extension | Native-tested | freeform | model | v2406: all variants; 41 checks in acceptance/freeform/surface_checks.json |
| nx_create_sheet_metal_callout | Experimental | sheet_metal | model | no acceptance evidence: hidden and refused in production |
| nx_create_sheet_metal_feature | Native-tested | sheet_metal | model | v2406: bead, bend, bend_taper, break_corner, bridge_bend, bulge_relief, closed_corner, contour_flange, convert, dimple, drawn_cutout, edge_rip, flange, flat_pattern, flat_solid, from_solid, gusset, hem, jog, lofted_flange, louver, normal_cutout, rebend, resize_bend_angle, resize_bend_radius, resize_neutral_factor, solid_punch, tab, three_bend_corner, unbend (untested: advanced_flange, joggle, lightening_cutout, variational_flange); 11 checks in acceptance/sheet_metal/three_bend_corner.json |
| nx_create_sheet_sketch | Native-tested | drawing | model | v2406: all variants; 53 checks in acceptance/drawing/sheet_sketch_hatch.json |
| nx_create_sheet_symbol | Native-tested | drawing | model | v2406: all variants; 52 checks in acceptance/drawing/symbols.json |
| nx_create_shell | Native-tested | modeling | model | v2406: tested; 6 checks in acceptance/modeling/shell_alternate.json |
| nx_create_simplified_assembly | Native-tested | assembly | model | v2406: tested; 20 checks in acceptance/assembly/simplify.json |
| nx_create_sketch | Native-tested | sketch | model | v2406: all variants; 26 checks in acceptance/sketch/sketch_one_call.json |
| nx_create_sketch_constraints | Native-tested | sketch | model | v2406: all variants; 84 checks in acceptance/sketch/sketch_safety.json |
| nx_create_sketch_copy | Native-tested | sketch | model | v2406: all variants; 84 checks in acceptance/sketch/sketch_safety.json |
| nx_create_sketch_corner | Native-tested | sketch | model | v2406: all variants; 84 checks in acceptance/sketch/sketch_safety.json |
| nx_create_sketch_curves | Native-tested | sketch | model | v2406: all variants; 18 checks in acceptance/sketch/sketch_spline.json |
| nx_create_sketch_dimensions | Native-tested | sketch | model | v2406: all variants; 84 checks in acceptance/sketch/sketch_safety.json |
| nx_create_sketch_projection | Native-tested | sketch | model | v2406: all variants; 84 checks in acceptance/sketch/sketch_safety.json |
| nx_create_spline | Native-tested | freeform | model | v2406: all variants; 42 checks in acceptance/freeform/spline.json |
| nx_create_surface_analysis | Native-tested | inspection | model | v2406: all variants; 26 checks in acceptance/inspection/surface_quality_values.json |
| nx_create_sweep | Native-tested | modeling | model | v2406: tested; 29 checks in acceptance/modeling/sweep.json |
| nx_create_sweep_along_guide | Native-tested | modeling | model | v2406: tested; 9 checks in acceptance/modeling/sweep_guide.json |
| nx_create_swept_volume | Native-tested | freeform | model | v2406: tested; 31 checks in acceptance/freeform/swept_volume.json |
| nx_create_text | Native-tested | modeling | model | v2406: tested; 22 checks in acceptance/modeling/text.json |
| nx_create_thicken | Native-tested | freeform | model | v2406: tested; 41 checks in acceptance/freeform/surface_checks.json |
| nx_create_thread | Native-tested | modeling | model | v2406: tested; 48 checks in acceptance/modeling/thread_natural_args.json |
| nx_create_topology_condition | Native-tested | optimization | model | v2406: extrude, fixed, force, keep_out, member_size, pinned, pressure, symmetry (untested: keep_in); 17 checks in acceptance/optimization/timeline.json |
| nx_create_topology_environment | Native-tested | optimization | model | v2406: all variants; 17 checks in acceptance/optimization/timeline.json |
| nx_create_topology_study | Experimental | optimization | model | no acceptance evidence: hidden and refused in production |
| nx_create_transform | Native-tested | modeling | model | v2406: tested; 19 checks in acceptance/modeling/transform_copy_order.json |
| nx_create_trim_body | Native-tested | modeling | model | v2406: all variants; 19 checks in acceptance/modeling/trim_body.json |
| nx_create_trim_curve | Native-tested | freeform | model | v2406: all variants; 85 checks in acceptance/freeform/curve_edit.json |
| nx_create_trim_sheet | Native-tested | freeform | model | v2406: tested; 31 checks in acceptance/freeform/trim_sheet.json |
| nx_create_tube | Native-tested | modeling | model | v2406: tested; 3 checks in acceptance/modeling/tube_inch.json |
| nx_create_unsew | Native-tested | modeling | model | v2406: all variants; 5 checks in acceptance/modeling/repair_checks.json |
| nx_create_untrim | Native-tested | modeling | model | v2406: tested; 35 checks in acceptance/modeling/repair.json |
| nx_create_variable_offset | Native-tested | freeform | model | v2406: tested; 36 checks in acceptance/freeform/variable_offset.json |
| nx_create_variational_sweep | Native-tested | modeling | model | v2406: tested; 9 checks in acceptance/modeling/sweep_guide_variational.json |
| nx_create_wave_link | Native-tested | assembly | model | v2406: all variants; 79 checks in acceptance/assembly/wave_links.json |
| nx_create_wrap_geometry | Native-tested | modeling | model | v2406: tested; 3 checks in acceptance/modeling/wrap_checks.json |
| nx_delete_clearance_set | Native-tested | assembly | model | v2406: tested; 66 checks in acceptance/assembly/clearance_sets.json |
| nx_delete_decal | Native-tested | display | model | v2406: tested; 62 checks in acceptance/display/visual_materials.json |
| nx_delete_kf_rule | Native-tested | parts_files | model | v2406: tested; 54 checks in acceptance/parts_files/kf_merge.json |
| nx_delete_objects | Native-tested | session | model | v2406: tested; 33 checks in acceptance/session/delete_objects.json |
| nx_delete_results | Native-tested | session | file | v2406: tested; 21 checks in acceptance/session/results_scope.json |
| nx_download_file | Native-tested | parts_files | read | v2406: all variants; 36 checks in acceptance/parts_files/download_file.json |
| nx_edit_annotation | Native-tested | drawing | model | v2406: tested; 30 checks in acceptance/drawing/edit_annotation.json |
| nx_edit_arrangement | Native-tested | assembly | model | v2406: all variants; 24 checks in acceptance/assembly/assembly_cut.json |
| nx_edit_assembly_constraint | Native-tested | assembly | model | v2406: tested; 82 checks in acceptance/assembly/constraints.json |
| nx_edit_blend_order | Native-tested | modeling | model | v2406: tested; 15 checks in acceptance/modeling/sync_blends.json |
| nx_edit_component | Native-tested | assembly | model | v2406: tested; 42 checks in acceptance/assembly/new_component.json |
| nx_edit_component_group | Native-tested | assembly | model | v2406: tested; 88 checks in acceptance/assembly/weight_management.json |
| nx_edit_component_pattern | Native-tested | assembly | model | v2406: tested; 88 checks in acceptance/assembly/patterns.json |
| nx_edit_dimension | Native-tested | drawing | model | v2406: tested; 79 checks in acceptance/drawing/edit_dimension.json |
| nx_edit_drawing_table | Native-tested | drawing | model | v2406: all variants; 57 checks in acceptance/drawing/title_block.json |
| nx_edit_drawing_view | Native-tested | drawing | model | v2406: tested; 36 checks in acceptance/drawing/view_visibility.json |
| nx_edit_edges | Native-tested | modeling | model | v2406: all variants; 14 checks in acceptance/modeling/sync_edges.json |
| nx_edit_explosion | Native-tested | assembly | model | v2406: tested; 161 checks in acceptance/assembly/explosions.json |
| nx_edit_explosion_trace | Native-tested | assembly | model | v2406: tested; 161 checks in acceptance/assembly/explosions.json |
| nx_edit_face_region | Native-tested | modeling | model | v2406: all variants; 10 checks in acceptance/modeling/sync_region.json |
| nx_edit_face_relation | Native-tested | modeling | model | v2406: all variants; 36 checks in acceptance/modeling/sync_relate.json |
| nx_edit_faces | Native-tested | modeling | model | v2406: all variants; 33 checks in acceptance/modeling/sync_edits.json |
| nx_edit_feature | Native-tested | modeling | model | v2406: tested; 45 checks in acceptance/modeling/expression_natural_args.json |
| nx_edit_section_view | Native-tested | display | view | v2406: tested; 24 checks in acceptance/display/sections_switch.json |
| nx_edit_sketch_curves | Native-tested | sketch | model | v2406: all variants; 52 checks in acceptance/sketch/sketch_natural_ops.json |
| nx_export_assembly | Native-tested | parts_files | file | v2406: all variants; 15 checks in acceptance/parts_files/subfolder_components.json |
| nx_export_bend_list | Native-tested | sheet_metal | file | v2406: tested; 43 checks in acceptance/sheet_metal/default_followers.json |
| nx_export_drawing | Native-tested | drawing | file | v2406: tested; 19 checks in acceptance/drawing/out_of_date_views.json |
| nx_export_explosion_animation | Native-tested | assembly | file | v2406: tested; 161 checks in acceptance/assembly/explosions.json |
| nx_export_expressions | Native-tested | modeling | file | v2406: tested; 70 checks in acceptance/modeling/expression_files.json |
| nx_export_flat_pattern | Native-tested | sheet_metal | file | v2406: tested; 14 checks in acceptance/sheet_metal/inch.json |
| nx_export_inspection_report | Native-tested | inspection | file | v2406: tested; 52 checks in acceptance/inspection/measure_details.json |
| nx_export_mass_report | Native-tested | inspection | file | v2406: tested; 52 checks in acceptance/inspection/measure_details.json |
| nx_export_model | Native-tested | parts_files | file | v2406: dwg, dxf, iges, jt, parasolid, step, stl (untested: step_ap242); 41 checks in acceptance/parts_files/export_model_assembly.json |
| nx_export_profile | Native-tested | parts_files | file | v2406: tested; 33 checks in acceptance/parts_files/profile_options.json |
| nx_find_geometry | Native-tested | inspection | read | v2406: tested; 52 checks in acceptance/inspection/measure_details.json |
| nx_find_holes | Native-tested | inspection | read | v2406: tested; 52 checks in acceptance/inspection/measure_details.json |
| nx_finish_preview | Native-tested | session | model | v2406: all variants; 23 checks in acceptance/session/preview_sketch_extrude.json |
| nx_finish_sketch | Native-tested | sketch | model | v2406: tested; 55 checks in acceptance/sketch/sketch_natural_args.json |
| nx_get_appearance | Native-tested | display | read | v2406: tested; 69 checks in acceptance/display/natural_values.json |
| nx_get_attributes | Native-tested | parts_files | read | v2406: tested; 181 checks in acceptance/parts_files/attributes.json |
| nx_get_bend_list | Native-tested | sheet_metal | read | v2406: tested; 43 checks in acceptance/sheet_metal/default_followers.json |
| nx_get_camera | Native-tested | display | read | v2406: tested; 54 checks in acceptance/display/view_directions.json |
| nx_get_dimension | Native-tested | drawing | read | v2406: tested; 26 checks in acceptance/drawing/get_dimension.json |
| nx_get_drawing_table | Native-tested | drawing | read | v2406: tested; 57 checks in acceptance/drawing/title_block.json |
| nx_get_drawing_view | Native-tested | drawing | read | v2406: tested; 36 checks in acceptance/drawing/view_visibility.json |
| nx_get_explosion | Native-tested | assembly | read | v2406: tested; 161 checks in acceptance/assembly/explosions.json |
| nx_get_feature | Native-tested | modeling | read | v2406: tested; 31 checks in acceptance/modeling/results_reported.json |
| nx_get_kf_rules | Native-tested | parts_files | read | v2406: tested; 39 checks in acceptance/parts_files/kf_rules.json |
| nx_get_material | Native-tested | modeling | read | v2406: tested; 31 checks in acceptance/modeling/results_reported.json |
| nx_get_model_summary | Native-tested | inspection | read | v2406: tested; 19 checks in acceptance/inspection/self_overlap_face_limit.json |
| nx_get_operation | Native-tested | session | read | v2406: tested; 62 checks in acceptance/session/status_operations.json |
| nx_get_part_family | Native-tested | parts_files | read | v2406: tested; 29 checks in acceptance/parts_files/part_family.json |
| nx_get_result | Native-tested | session | read | v2406: tested; 85 checks in acceptance/session/results.json |
| nx_get_sheet_metal | Native-tested | sheet_metal | read | v2406: tested; 14 checks in acceptance/sheet_metal/inch.json |
| nx_get_sheet_metal_defaults | Native-tested | sheet_metal | read | v2406: tested; 25 checks in acceptance/sheet_metal/flat_pattern_axes.json |
| nx_get_sketch | Native-tested | sketch | read | v2406: tested; 55 checks in acceptance/sketch/sketch_natural_args.json |
| nx_get_status | Native-tested | session | read | v2406: tested; 62 checks in acceptance/session/status_operations.json |
| nx_get_topology_study | Native-tested | optimization | read | v2406: tested; 45 checks in acceptance/optimization/environment.json |
| nx_get_true_shading | Native-tested | display | read | v2406: tested; 39 checks in acceptance/display/lights_background.json |
| nx_get_visual_material | Native-tested | display | read | v2406: tested; 62 checks in acceptance/display/visual_materials.json |
| nx_import_expressions | Native-tested | modeling | model | v2406: all variants; 70 checks in acceptance/modeling/expression_files.json |
| nx_import_model | Native-tested | parts_files | model | v2406: dxf_dwg, iges, parasolid, step, stl (untested: catia_v5); 33 checks in acceptance/parts_files/profile_options.json |
| nx_import_points | Native-tested | freeform | model | v2406: tested; 39 checks in acceptance/freeform/curve_fit.json |
| nx_list_arrangements | Native-tested | assembly | read | v2406: tested; 61 checks in acceptance/assembly/arrangements.json |
| nx_list_checkpoints | Native-tested | session | read | v2406: tested; 41 checks in acceptance/session/undo_survives_load.json |
| nx_list_clearance_sets | Native-tested | assembly | read | v2406: tested; 66 checks in acceptance/assembly/clearance_sets.json |
| nx_list_component_groups | Native-tested | assembly | read | v2406: tested; 88 checks in acceptance/assembly/weight_management.json |
| nx_list_components | Native-tested | assembly | read | v2406: tested; 146 checks in acceptance/assembly/components.json |
| nx_list_files | Native-tested | parts_files | read | v2406: tested; 23 checks in acceptance/parts_files/list_files.json |
| nx_list_lights | Native-tested | display | read | v2406: tested; 39 checks in acceptance/display/lights_background.json |
| nx_list_materials | Native-tested | modeling | read | v2406: tested; 42 checks in acceptance/modeling/material_library.json |
| nx_list_objects | Native-tested | inspection | read | v2406: tested; 52 checks in acceptance/inspection/measure_details.json |
| nx_list_parts | Native-tested | parts_files | read | v2406: tested; 19 checks in acceptance/parts_files/list_parts.json |
| nx_list_thread_sizes | Native-tested | modeling | read | v2406: tested; 48 checks in acceptance/modeling/thread_natural_args.json |
| nx_list_view_edges | Native-tested | drawing | read | v2406: tested; 36 checks in acceptance/drawing/view_visibility.json |
| nx_list_visual_materials | Native-tested | display | read | v2406: tested; 62 checks in acceptance/display/visual_materials.json |
| nx_measure_angle | Native-tested | inspection | read | v2406: tested; 68 checks in acceptance/inspection/measure.json |
| nx_measure_assembly_mass | Native-tested | inspection | read | v2406: tested; 69 checks in acceptance/inspection/assembly_weight.json |
| nx_measure_bounds | Native-tested | inspection | read | v2406: tested; 68 checks in acceptance/inspection/measure.json |
| nx_measure_distance | Native-tested | inspection | read | v2406: tested; 68 checks in acceptance/inspection/measure.json |
| nx_measure_mass | Native-tested | inspection | read | v2406: tested; 52 checks in acceptance/inspection/measure_details.json |
| nx_measure_section | Native-tested | inspection | read | v2406: tested; 39 checks in acceptance/inspection/section.json |
| nx_measure_section_inertia | Native-tested | inspection | read | v2406: tested; 43 checks in acceptance/inspection/section_inertia.json |
| nx_measure_wall_thickness | Native-tested | inspection | read | v2406: tested; 52 checks in acceptance/inspection/measure_details.json |
| nx_open_part | Native-tested | parts_files | session | v2406: tested; 15 checks in acceptance/parts_files/subfolder_components.json |
| nx_preview_changes | Native-tested | session | session | v2406: tested; 23 checks in acceptance/session/preview_sketch_extrude.json |
| nx_redo_operation | Native-tested | session | session | v2406: tested; 71 checks in acceptance/session/redo_replay.json |
| nx_resolve_reference | Native-tested | inspection | read | v2406: tested; 18 checks in acceptance/inspection/resolve_two_parts.json |
| nx_restore_checkpoint | Native-tested | session | model | v2406: tested; 41 checks in acceptance/session/undo_survives_load.json |
| nx_restore_display | Native-tested | display | view | v2406: tested; 58 checks in acceptance/display/visibility_assembly.json |
| nx_run_batch | Native-tested | session | model | v2406: tested; 43 checks in acceptance/session/run_batch.json |
| nx_run_clearance_set | Native-tested | assembly | model | v2406: tested; 66 checks in acceptance/assembly/clearance_sets.json |
| nx_run_motion_sweep | Native-tested | motion | read | v2406: all variants; 38 checks in acceptance/motion/steering_linkage.json |
| nx_run_topology_study | Native-tested | optimization | model | v2406: tested; 82 checks in acceptance/optimization/contour.json |
| nx_save_part | Native-tested | parts_files | file | v2406: tested; 18 checks in acceptance/session/refs_survive_undo_save.json |
| nx_save_presentation | Native-tested | display | file | v2406: tested; 12 checks in acceptance/display/presentation_paths.json |
| nx_set_appearance | Native-tested | display | view | v2406: tested; 69 checks in acceptance/display/natural_values.json |
| nx_set_arrangement_component | Native-tested | assembly | model | v2406: tested; 24 checks in acceptance/assembly/assembly_cut.json |
| nx_set_attributes | Native-tested | parts_files | model | v2406: tested; 44 checks in acceptance/parts_files/parts_checks.json |
| nx_set_camera | Native-tested | display | view | v2406: tested; 54 checks in acceptance/display/view_directions.json |
| nx_set_component_deformation | Native-tested | assembly | model | v2406: tested; 68 checks in acceptance/assembly/deformable.json |
| nx_set_component_mass | Native-tested | assembly | model | v2406: tested; 88 checks in acceptance/assembly/weight_management.json |
| nx_set_component_pose | Native-tested | assembly | model | v2406: all variants; 58 checks in acceptance/assembly/constraint_values.json |
| nx_set_current_feature | Native-tested | modeling | model | v2406: tested; 18 checks in acceptance/modeling/current_feature.json |
| nx_set_explosion_display | Native-tested | assembly | model | v2406: tested; 161 checks in acceptance/assembly/explosions.json |
| nx_set_expression | Native-tested | modeling | model | v2406: tested; 45 checks in acceptance/modeling/expression_natural_args.json |
| nx_set_feature_order | Native-tested | modeling | model | v2406: tested; 11 checks in acceptance/modeling/feature_order_rollback.json |
| nx_set_feature_suppression | Native-tested | modeling | model | v2406: tested; 16 checks in acceptance/modeling/feature_suppression.json |
| nx_set_highlight | Native-tested | display | view | v2406: tested; 10 checks in acceptance/display/highlight.json |
| nx_set_kf_rule | Native-tested | parts_files | model | v2406: all variants; 39 checks in acceptance/parts_files/kf_rules.json |
| nx_set_light | Native-tested | display | model | v2406: tested; 39 checks in acceptance/display/lights_background.json |
| nx_set_lighting_preset | Native-tested | display | model | v2406: tested; 39 checks in acceptance/display/lights_background.json |
| nx_set_material | Native-tested | modeling | model | v2406: tested; 5 checks in acceptance/modeling/material_undo.json |
| nx_set_name | Native-tested | session | model | v2406: tested; 99 checks in acceptance/session/history.json |
| nx_set_sheet_borders | Native-tested | drawing | model | v2406: tested; 36 checks in acceptance/drawing/borders.json |
| nx_set_sheet_image | Native-tested | drawing | model | v2406: tested; 7 checks in acceptance/drawing/sheet_image_tiff.json |
| nx_set_sheet_metal_defaults | Native-tested | sheet_metal | model | v2406: tested; 14 checks in acceptance/sheet_metal/inch.json |
| nx_set_topology_contour | Native-tested | optimization | view | v2406: tested; 82 checks in acceptance/optimization/contour.json |
| nx_set_true_shading | Native-tested | display | model | v2406: tested; 62 checks in acceptance/display/visual_materials.json |
| nx_set_view_sectioning | Native-tested | drawing | model | v2406: tested; 25 checks in acceptance/drawing/bolt_circle_section_in_view.json |
| nx_set_view_settings | Native-tested | drawing | model | v2406: tested; 36 checks in acceptance/drawing/view_visibility.json |
| nx_set_visibility | Native-tested | display | view | v2406: tested; 58 checks in acceptance/display/visibility_assembly.json |
| nx_set_visual_material | Native-tested | display | model | v2406: tested; 62 checks in acceptance/display/visual_materials.json |
| nx_undo_operation | Native-tested | session | model | v2406: tested; 41 checks in acceptance/session/undo_survives_load.json |
| nx_update_annotations | Native-tested | drawing | model | v2406: tested; 8 checks in acceptance/drawing/update_annotations.json |
| nx_update_assembly | Native-tested | assembly | model | v2406: tested; 29 checks in acceptance/assembly/update_assembly.json |
| nx_update_kf_rules | Native-tested | parts_files | model | v2406: tested; 39 checks in acceptance/parts_files/kf_rules.json |
| nx_update_model | Native-tested | modeling | model | v2406: tested; 18 checks in acceptance/modeling/update_model.json |
| nx_upload_file | Native-tested | parts_files | file | v2406: tested; 62 checks in acceptance/parts_files/upload_file.json |

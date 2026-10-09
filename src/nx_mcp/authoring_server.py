"""Public signatures and contracts for authoring/review tools."""

from __future__ import annotations

from typing import Annotated, Any, Literal

try:
    from pydantic import Field

    PositiveLength = Annotated[float, Field(gt=0)]
except ImportError:  # NX-side import: only the sidecar validates these signatures.
    PositiveLength = float  # type: ignore[misc]

READ_ONLY = {
    "nx_list_explosions",
    "nx_explosion_info",
    "nx_find_geometry",
    "nx_list_expressions",
    "nx_model_health",
    "nx_model_summary",
    "nx_resolve_geometry",
    "nx_recognize_holes",
    "nx_list_component_patterns",
}
NON_MODEL = {
    "nx_highlight_objects",
    "nx_save_presentation",
    "nx_inspection_report",
    "nx_preview_change",
}


def nx_find_geometry(
    owner: str | None = None,
    kind: Literal["face", "edge"] = "face",
    geometry_type: Literal["any", "plane", "cylinder", "circle", "line"] = "any",
    normal: list[float] | None = None,
    radius: float | None = None,
    near: list[float] | None = None,
    order: Literal["nearest", "highest", "lowest"] = "nearest",
    axis: Literal["X", "Y", "Z"] = "Z",
    tolerance: float = 0.001,
    offset: int = 0,
    limit: int = 50,
):
    """Find geometry within a body/feature/component or full assembly. Coordinates and radii use work-part units/frame. Nearest uses native BREP point-to-face/edge minimum distance and closest points; highest/lowest use conservative bounds centers. Returns a reusable geometric selector; resolve it explicitly after edits with nx_resolve_geometry. nearest requires near=[x,y,z]. Normal filter requires planar faces; tolerance is 1-dot for normals and absolute length for radii. Returns paginated candidates; never silently selects one."""


def nx_highlight_objects(objects: list[str]):
    """Preview candidate body/face/edge/curve/component references using native selection highlighting. Replaces MCP-owned highlights; clear with nx_clear_highlights. No persistent recoloring."""


def nx_list_expressions(name_contains: str | None = None, offset: int = 0, limit: int = 50):
    """Paginate work-part expressions, formulas, numeric values in expression units, editability and stored immediate dependencies. Names are case-sensitive. Non-numeric values are null. Conditional dependencies may be incomplete."""


def nx_model_health(scope: Literal["part", "assembly"] = "part", offset: int = 0, limit: int = 50):
    """Inspect native feature errors/warnings, suppression, unloaded prototype availability and UF body consistency. Assembly scope checks unique loaded unsuppressed prototypes. Does not force rebuild or certify design intent. Paginate issues; counts cover the whole query."""


def nx_component_action(
    component: str,
    action: Literal["rename", "suppress", "unsuppress", "remove", "replace"],
    name: str | None = None,
    part_path: str | None = None,
):
    """Rename, replace, remove, suppress or unsuppress an immediate child occurrence of the work assembly. rename requires name; replace requires existing workspace .prt part_path and replaces only this occurrence while retaining relationships. Other actions reject those fields. Suppression affects all arrangements. Checks unchanged placement; native errors roll back. Prototype files are not deleted by remove."""


def nx_pattern_components(component: str, direction: list[float], spacing: float, count: int):
    """Create 2–100 total occurrences including the seed at a positive pitch in work-part units along normalized direction. Seed must be an unsuppressed immediate child. Returns independent positioned occurrences, not a native associative pattern. Runs serially in one rollback transaction."""


def nx_set_camera(rotation: list[list[float]], origin: list[float], scale: float):
    """Set absolute NX viewport camera. rotation is right-handed orthonormal row-major 3x3 whose columns are the NX view axes (screen right, screen up, toward the viewer) in display-part coordinates. origin is NX's native view origin, the same value nx_view_info returns: the negated view-space coordinates of the point shown at the viewport center, so to center display-part point P use origin = -(P.x_axis, P.y_axis, P.z_axis). scale is positive absolute NX scale. Work and display part must match. Returns actual camera."""


def nx_save_presentation(path: str):
    """Save camera, one active section, explicit appearance and loaded assembly body/component visibility into a new workspace JSON file. Includes per-face colors/transparency and journal locators. Does not save the CAD part. Excludes datum visibility, materials and inherited-override semantics."""


def nx_restore_presentation(path: str):
    """Restore a saved presentation in its original owner part. Preflight all journal references before mutation; stale references reject restoration. Geometry is unchanged, but display attributes can mark the part modified. Across topology revisions verify journal references still denote intended geometry."""


def nx_inspection_report(
    path: str,
    objects: list[str] | None = None,
    minimum_clearance: float = 0.0,
    max_pairs: int = 100,
    include_clear: bool = False,
    capture: bool = True,
    section_planes: list[dict[str, list[float]]] | None = None,
):
    """Create a new workspace ZIP containing HTML/JSON clearance results, checksums, native viewport PNG, up to eight flagged-pair close-ups and six section screenshots. section_planes entries require origin and normal in work-part coordinates. Restores temporary view/visibility/section changes. max_pairs caps native clearance work; report does not imply unchecked pairs are clear. Retrieve with nx_download_file."""


def nx_model_summary(
    section: Literal["overview", "components", "features", "expressions", "sketches"] = "overview",
    offset: int = 0,
    limit: int = 50,
):
    """Compact work-part summary: counts, bounds, native health and paginated assembly hierarchy, feature parents/parameters, expressions or sketch frames. Owned-body and occurrence counts are separate. Does not infer missing design dimensions."""


def nx_preview_change(operations: list[dict[str, Any]], capture: bool = True):
    """Temporarily apply 1–25 edits under a native checkpoint, return before/after bounds/volume/parameters/health and optional screenshot, then roll back before returning. Each entry has method and params. Supports nx_set_component_transform and nx_reposition_component. Returned geometry IDs are stale after rollback. Preview is session-scoped and expires on intervening mutations or manual handoff."""


def nx_finish_preview(preview_id: str, action: Literal["accept", "discard"]):
    """Accept by atomically reapplying stored preview edits only if its part and mutation epoch are unchanged, or discard the stored plan. Preview already restored original geometry. Accepted edits remain unsaved and undoable. Stable operation_id prevents double application."""


def nx_resolve_geometry(selector: dict[str, Any], tie_tolerance: float = 0.001):
    """Re-evaluate a selector returned by nx_find_geometry in its original owner part. Returns a fresh face/edge ID only if the best match is unique within tie_tolerance in part units. No match, ambiguous rank or stale owner is an explicit error. Survives edits/reopen by geometric rule, not a promise of persistent topological identity. Whole-part rules may select new geometry that now satisfies the rule."""


def nx_recognize_holes(owner: str | None = None, offset: int = 0, limit: int = 50):
    """Recognize inward cylindrical BREP faces and return bore radius, axis, angular coverage and coaxial groups in work-part coordinates/units. Partial cylindrical faces are identified explicitly. Does not infer threads, manufacturing features, blind/through termination or fit classes."""


def nx_native_component_pattern(component: str, direction: list[float], spacing: float, count: int):
    """Create a native associative linear component pattern with 2–100 total occurrences including one immediate unsuppressed seed. Direction is normalized in work-part coordinates; spacing is positive part units. Native builder and pattern members are read back. Rollback on update/count failure. Existing nx_pattern_components retains independent-instance behavior."""


def nx_edit_component_pattern(
    pattern: str,
    spacing: float | None = None,
    count: int | None = None,
    count_y: int | None = None,
    spacing_y: float | None = None,
    angle: float | None = None,
):
    """Edit an associative single-seed rectangular or circular component pattern. Rectangular supports count/spacing and existing second-direction count_y/spacing_y. Circular supports count/angular pitch in degrees. Counts include seed and total instances must be <=100. Return native expressions and all actual placements; invalid edits roll back."""


def nx_list_component_patterns():
    """Enumerate native work-assembly component patterns, IDs, native type, association, count/pitch expressions for linear patterns and member occurrence poses. Independent instances are not patterns."""


def nx_mass_properties(
    body: str | None = None, scope: Literal["auto", "part", "assembly"] = "auto"
):
    """Measure native solid mass, volume, area, center of gravity and centroidal inertia using assigned densities. Outputs kg, m and kg*m^2 in the work-part WCS, including that WCS's origin/basis and native error estimates. Assembly scope includes loaded unsuppressed occurrence geometry. Overlaps are summed, not geometrically united; review assigned densities."""


READ_ONLY.add("nx_mass_properties")


def nx_component_array(
    component: str,
    pattern_type: Literal["rectangular", "circular"],
    count: int,
    spacing: float | None = None,
    direction: list[float] | None = None,
    count_y: int = 1,
    spacing_y: float | None = None,
    direction_y: list[float] | None = None,
    center: list[float] | None = None,
    axis: list[float] | None = None,
    angle: float | None = None,
):
    """Create an associative native component array with 2–100 total instances including seed. Rectangular uses direction/spacing and optional count_y/direction_y/spacing_y. Circular uses center/axis and positive angular pitch in degrees; positions must not wrap to duplicate the seed. Coordinates/lengths use work-part frame/units. Seed must be an unsuppressed immediate child. Parameters for the other pattern type are rejected."""


def nx_render_view(
    path: str | None = None,
    width: int = 1600,
    height: int = 1000,
    background: Literal["white", "original", "transparent", "color"] = "white",
    color: list[float] | None = None,
    style: Literal["studio", "shaded", "shaded_with_edges"] = "studio",
    lighting: int | None = None,
):
    """Render the current interactive NX camera to a new PNG using native Studio image capture. Exact width/height in pixels (128–4096), optional native lighting preset 1–5, and original/white/transparent/custom RGB [0,1] background. Restore temporary view style and lighting afterward. Return camera, checksum, artifact path and inline image. Uses existing model appearance; does not invent physical materials. Requires installed native rendering capability."""


def nx_list_assembly_constraints():
    """Inspect native constraints defined in the work assembly: typed IDs, component/geometry references, alignment, suppression, expressions and native solver status. Does not infer that unconstrained components are fixed."""


def nx_assembly_constraint(
    constraint_type: Literal[
        "fix", "touch", "distance", "parallel", "perpendicular", "angle", "concentric"
    ],
    component: str,
    geometry: str | None = None,
    target_component: str | None = None,
    target_geometry: str | None = None,
    value: float | None = None,
    alignment: Literal["infer", "same", "opposite"] = "infer",
):
    """Create a persistent native assembly constraint between unsuppressed immediate child components. Fix takes only component. Other types require geometry and target_geometry occurrence face/edge IDs owned by the respective components. Distance uses part units, angle degrees. Native solver must report Solved or operation rolls back. Can move components while solving; reacquire poses afterward."""


def nx_edit_assembly_constraint(
    constraint: str,
    value: float | None = None,
    suppressed: bool | None = None,
    alignment: Literal["infer", "same", "opposite"] | None = None,
):
    """Edit an existing typed assembly constraint's distance/angle, suppression, or alignment. Solve natively and roll back unsatisfied edits. Values are absolute, in part length units or degrees. Component positions may change during solving."""


READ_ONLY.add("nx_list_assembly_constraints")


def nx_mate_component(
    component: str,
    mate_type: Literal["touch", "align", "orient", "center", "align_angle"],
    references: list[str] | None = None,
    offset: float = 0.0,
):
    """Create a native assembly mate using two occurrence face/edge references [moving,target]. Touch uses opposite alignment; align uses same alignment; nonzero offset creates a distance. Orient is parallel, center is concentric between circular edges, align_angle uses offset in degrees. The solver may move either component; fix the target first to keep it in place. Other lengths use work-part units. Prefer nx_assembly_constraint for explicit constraint semantics."""


def nx_create_explosion(name: str):
    """Create a named native explosion in the work/display assembly, initially at assembled positions. Names are unique case-insensitively, nonempty and <=132 characters. Return a typed explosion ID. Requires assemblies license. Does not reposition actual components or show the explosion automatically."""


def nx_list_explosions():
    """List native explosions owned by the work part, typed IDs, names and referencing model/drawing views. No model or view changes."""


def nx_explosion_info(explosion: str, offset: int = 0, limit: int = 50):
    """Inspect an explosion by typed ID. Paginate occurrences (limit 1–200), actual exploded and assembled positions, rotations, paths and suppression. Positions are absolute work-assembly coordinates in part units; matrices are right-handed row-major. Ordinary body/clearance/mass tools still measure assembled geometry."""


def nx_edit_explosion(
    explosion: str,
    placements: list[dict[str, Any]] | None = None,
    reset_components: list[str] | None = None,
):
    """Atomically replace absolute exploded poses or reset selected components to inherited parent-explosion positions. Provide 1–1000 unique occurrences across placements and reset_components. Each placement has component ID, translation=[x,y,z] in assembly coordinates/part units, and optional right-handed orthonormal row-major rotation_matrix; omitted rotation retains the current exploded world orientation at request start. Parents apply before children; descendants inherit parent changes. Resolves and validates every item before mutation, verifies final native poses and unchanged actual placements, and updates associated drawing views. Safe repeated absolute placement; use operation_id for transport retries."""


def nx_show_explosion(
    explosion: str | None = None, drawing_view: str | None = None, model_view: str | None = None
):
    """Display a native explosion, or assembled positions when explosion is null. Without drawing_view, return to 3D modeling and fit the work view. With an owned drawing-view ID, set that view's explosion association and update it. model_view alternatively targets an existing saved model-view ID without switching the visible view. The two targets are mutually exclusive. Work and display parts must match; finish active sketches first. Changes view presentation, never actual component placement."""


def nx_delete_explosion(explosion: str):
    """Delete a native explosion only when no model/drawing views reference it. Detach dependent views with nx_show_explosion(explosion=null) first. Native deletion is transactional; returned reference becomes stale. Actual assembly components are retained."""


EXPLOSION_PLACEMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["component", "translation"],
    "properties": {
        "component": {
            "type": "string",
            "description": "Typed occurrence reference in this explosion.",
        },
        "translation": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number"}},
        "rotation_matrix": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number"}},
        },
    },
}

"""Uniform MCP envelopes and workspace-scoped artifacts for the NX 2606 bridge."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import sys
import uuid
from contextvars import ContextVar
from typing import Annotated, Literal

from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from nx_mcp import (
    assembly_documentation_server,
    authoring_server,
    documentation_editing_server,
    freeform_server,
    manufacturing_server,
    sheet_metal_server,
)
from nx_mcp.feature_groups import server_modules as feature_group_modules
from nx_mcp.output_schemas import output_schema
from nx_mcp.recovery import OperationStore
from nx_mcp.runtime import NXToolError
from nx_mcp.workspace import WorkspaceViolation

# The agent's short note on why it makes a call (set by nx_invoke). It travels to
# the NX control panel with the bridge request and is never a handler argument.
REQUEST_INTENT: ContextVar[str | None] = ContextVar("nx_mcp_request_intent", default=None)


def nx_list_components(
    compact: bool = False,
    include_transforms: bool = True,
    name_contains: str | None = None,
    suppressed: bool | None = None,
    offset: int = 0,
    limit: int | None = None,
):
    """List recursive component occurrences without loading prototypes. load_state reports fully_loaded, partially_loaded or unloaded; part_path can be null if NX cannot resolve it. Unloaded subassemblies may hide descendants. Use nx_open_part(load_components=true) for explicit recovery. compact retains occurrence paths and omits repeated identity metadata and legacy rotation. include_transforms=False omits pose fields. Name filtering is case-insensitive. Filters precede paging; total_count is matching rows. Defaults preserve existing full results."""


def nx_flat_pattern_orientation_edges(upward_face: str):
    """List current straight boundary edges of an owned planar sheet-metal web face, with endpoints and adjacent face IDs. Pass a returned edge ID as flat_pattern.x_axis_edge. Geometric eligibility only; NX validates the final feature. Does not mutate or invalidate references."""


def nx_list_reference_sets():
    """List work-part custom reference sets, exact direct members and automatic-add setting. Built-in Entire Part and Empty are listed separately."""


def nx_create_reference_set(name: str, objects: list[str]):
    """Create a custom reference set with explicit owned body/curve/datum/direct-component IDs. Use only solid body IDs to exclude prototype datums from assembly drawings. No automatic component membership; duplicate names are rejected. Changes the work part; save it to persist."""


def nx_set_component_reference_set(components: list[str], name: str):
    """Assign an exact existing prototype reference-set name to direct children of the work assembly. Activate a nested owning assembly before editing its children. Preflights every target; does not change prototype membership or component poses. Returns previous/current assignments. Save the assembly to persist."""


def nx_list_datums():
    """List work-part datum planes, axes and coordinate systems with IDs and blanked state. Does not recurse into component prototypes."""


def nx_set_datum_visibility(visible: bool = False):
    """Show/hide all owned datums and coordinate systems in the work/display part. Returns restore_id for nx_restore_display. To exclude component prototype datums from drawings, assign a body-only reference set instead. Changes can persist when saved."""


# Public schemas for tools whose NX handlers live in hardened.py,
# legacy_repairs.py and nx_bridge.py. Descriptions come from DESCRIPTIONS
# where one exists.
def nx_delete_feature(name: str):
    """Delete a feature."""


def nx_measure_angle(obj1: str, obj2: str):
    """Measure the angle between two objects."""


def nx_measure_distance(obj1: str, obj2: str):
    """Measure the distance between two objects."""


def nx_reposition_component(
    component: str,
    dx: float = 0.0,
    dy: float = 0.0,
    dz: float = 0.0,
    rx: float = 0.0,
    ry: float = 0.0,
    rz: float = 0.0,
):
    """Reposition a component by translation and rotation offsets."""


def nx_set_view(orientation: str):
    """Set the view to a named orientation."""


def nx_display_info(objects: list[str], count_only: bool = False):
    pass


def nx_set_display(
    objects: list[str],
    color_index: int | None = None,
    transparency: int | None = None,
    color: Literal[
        "red", "green", "blue", "yellow", "cyan", "magenta", "orange", "white", "black", "gray"
    ]
    | None = None,
):
    pass


def nx_set_visibility(objects: list[str], mode: Literal["show", "hide", "isolate"] = "show"):
    pass


def nx_restore_display(restore_id: str):
    pass


def nx_highlight_collisions(obj1: str, obj2: str, include_contact: bool = False):
    pass


def nx_clear_highlights():
    pass


def nx_list_sections():
    pass


def nx_section_view(
    origin: list[float],
    normal: list[float],
    section: str | None = None,
    name: str = "MCP section",
    cap: bool = True,
):
    pass


def nx_section_control(section: str, action: Literal["enable", "disable", "delete"]):
    pass


def nx_ui_control(mode: Literal["status", "manual", "agent"] = "status"):
    pass


def nx_view_info():
    pass


def nx_screenshot(
    path: str | None = None,
    width: int = 1600,
    height: int = 1000,
    background: Literal["white", "original", "transparent"] = "white",
    style: Literal["current", "shaded", "shaded_with_edges", "wireframe"] = "shaded_with_edges",
    fit: bool = False,
):
    pass


def nx_check_interference(obj1: str, obj2: str):
    pass


def nx_check_clearance(
    objects: list[str] | None = None,
    minimum_clearance: float = 0.0,
    max_pairs: int = 100,
    include_clear: bool = False,
):
    pass


# Signature-only definitions are used to publish the actual bridge arguments.
def nx_get_bounding_box(
    body: str | None = None,
    scope: Literal["auto", "part", "assembly"] = "auto",
    precision: Literal["conservative", "exact"] = "conservative",
):
    pass


def nx_checkpoint(label: str = "checkpoint"):
    pass


def nx_checkpoint_state():
    pass


def nx_rollback(checkpoint_id: str):
    pass


def nx_operation_status(operation_id: str):
    pass


def nx_cancel_operation(operation_id: str):
    pass


def nx_list_topology(
    body: str, face: str | None = None, include_adjacency: bool = False, compact: bool = False
):
    pass


def nx_measure_volume(body: str | None = None, scope: Literal["auto", "part", "assembly"] = "auto"):
    """Measure the volume of a body or all bodies."""


def nx_add_component(
    part_path: str,
    name: str | None = None,
    translation: list[float] | None = None,
    rotation_matrix: list[list[float]] | None = None,
):
    pass


def nx_set_component_transform(
    component: str, translation: list[float], rotation_matrix: list[list[float]]
):
    pass


def nx_read_result(
    result_id: str,
    field: str = "",
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
):
    """Read a stored oversized bridge result without repeating its operation. field is a JSON pointer; arrays/strings are paged. Nested omissions remain explicit. Snapshot expiry does not delete operation receipts."""


class BatchParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


Vector3 = Annotated[list[float], Field(min_length=3, max_length=3)]
Matrix3 = Annotated[list[Vector3], Field(min_length=3, max_length=3)]


class ComponentParameters(BatchParameters):
    part_path: str
    name: str | None = None
    translation: Vector3 | None = None
    rotation_matrix: Matrix3 | None = None


class TransformParameters(BatchParameters):
    component: str
    translation: Vector3
    rotation_matrix: Matrix3


class RepositionParameters(BatchParameters):
    component: str
    dx: float = 0
    dy: float = 0
    dz: float = 0
    rx: float = 0
    ry: float = 0
    rz: float = 0


class ComponentOperation(BatchParameters):
    method: Literal["nx_add_component"]
    params: ComponentParameters


class TransformOperation(BatchParameters):
    method: Literal["nx_set_component_transform"]
    params: TransformParameters


class RepositionOperation(BatchParameters):
    method: Literal["nx_reposition_component"]
    params: RepositionParameters


BatchOperation = Annotated[
    ComponentOperation | TransformOperation | RepositionOperation,
    Field(discriminator="method"),
]


def nx_batch(operations: Annotated[list[BatchOperation], Field(min_length=1, max_length=100)]):
    pass


def nx_capabilities(tool: str | None = None, prefix: str | None = None):
    pass


def nx_rename_object(object_id: str, name: str):
    pass


def nx_workspace_info():
    pass


DESCRIPTIONS = {
    "nx_workspace_info": "Discover the NX host workspace root and path rules. Paths refer to the NX machine, not the MCP client's filesystem. No session-wide current directory is changed.",
    "nx_delete_feature": "Delete a work-part feature by typed ID or unambiguous name using native update/undo. Dependent geometry may be deleted; inspect changes.deleted and reacquire topology afterward.",
    "nx_measure_angle": "Measure 0..180 degrees between typed work-part line, straight-edge or planar-face references. Uses line start/end, edge vertex order or outward face normals. Curved entities and component occurrences are unsupported; directions are not an oriented dihedral angle.",
    "nx_display_info": "Inspect color-table indices, blank state and face transparency for body, component, feature, face or curve references. Components expand to loaded occurrence geometry.",
    "nx_set_display": "Set an NX color index (1–216) or named color, and/or transparency (0 opaque, 100 transparent). Component/feature targets expand to bodies and faces, with a cap of 10000 unique objects. Preflight with nx_display_info(count_only=true); split larger selections after checking counts. Occurrence overrides do not recolor prototypes. Returns restore_id; restore in reverse order. Changes can persist on save.",
    "nx_set_visibility": "Show, hide or isolate body/component geometry. Isolation preserves a restorable snapshot and includes ancestor components. Reference curves and datum geometry are not isolated. Explicit show/hide also accepts curves. Returns restore_id.",
    "nx_restore_display": "Restore explicit appearance/visibility attributes using a same-session restore_id, in reverse order. All references are preflighted; manual handoff, rollback or close can make snapshots stale. Does not reset a part modified flag or remove inherited occurrence overrides.",
    "nx_highlight_collisions": "Measure native solid interference and highlight the involved body occurrences using NX selection highlighting. Replaces previous MCP highlights. Contacts are optional; clear pairs are never highlighted. Returns measured pairs and entity references. No persistent recoloring.",
    "nx_clear_highlights": "Remove only highlights created by MCP. Geometry, persistent colors and visibility are unchanged.",
    "nx_list_sections": "Inspect native dynamic sections and the active view clipping toggle in the display/work part.",
    "nx_section_view": "Create or edit a native single-plane section in visible NX. origin is in display-part units; normal is normalized in display-part coordinates. Solids are unchanged. Specify section ID to edit an existing active section. NX v2606 retains dot(point-origin, normal) <= 0; reversing normal reverses the retained side. Returns actual plane geometry.",
    "nx_section_control": "Enable, disable or delete the specified native section. Disabling turns off clipping when that section is active. Deletion removes the section object, not model solids.",
    "nx_ui_control": "Inspect UI activity without queueing behind native work, or switch control modes. While the engineer has control, mode=agent (like any other request) is shown in the NX MCP panel and waits up to 60 s for them to click 'Give control to agent'; NX_UI_DECLINED means they chose 'Not now', so ask in chat before retrying. mode=manual hands control back at once. Agent mode reserves NX input even when idle. Status is a timestamped snapshot, not proof that a native call is responsive. Manual handoff invalidates references/checkpoints. Taking control back cannot interrupt a native call.",
    "nx_view_info": "Return the displayed model view, camera matrix, scale, rendering style, and interactive state.",
    "nx_screenshot": "Export the actual interactive NX viewport as PNG and return an inline MCP image. Advisory 128–4096 pixel dimensions (NX can use the actual device size; response reports both), background, shaded/wireframe style and fit. When a drawing sheet is displayed, exports the sheet as displayed (capture_kind nx_drawing_sheet, drawing reference, camera null; style does not apply, fit fits the sheet). No desktop capture. Paths are workspace-relative; omit for a unique capture path.",
    "nx_check_interference": "Check native solid interference between two body/component references, including nested occurrence geometry. Return penetration/contact/clear, closest points and pairwise interference volumes in mm^3. Temporary solids are rolled back.",
    "nx_check_clearance": "Check distinct solid body occurrences in selected groups or the full assembly. minimum_clearance uses part units. Conservative bounds prune clear pairs; reported distances and interference use native geometry. max_pairs (default 100, at most 10000) caps the pairs that need native checks after pruning; above it the call is refused before any native check. Volumes are pairwise, not union volume.",
    "nx_get_bounding_box": "Native UF bounds; precision selects conservative or exact (exact requires axis-aligned WCS). auto includes recursive assembly geometry when present; part includes directly owned bodies; assembly includes both. Coordinates and units are work-part absolute.",
    "nx_checkpoint": "Create an in-session model undo checkpoint. NX v2606 saves expire native marks; create a new checkpoint after save. Restart/close also invalidates checkpoints.",
    "nx_checkpoint_state": "Inspect available checkpoint IDs and retained model-operation history. Read-only calls retain marks. Native NX save can expire them; availability is checked against NX.",
    "nx_rollback": "Rollback to an in-session checkpoint. Rejects rollback across mutations to unrelated parts. Reacquire object IDs afterward; save explicitly to persist.",
    "nx_operation_status": "Read durable request state without waiting on the NX thread: running, committed, failed, unknown. Unknown never authorizes blind retry.",
    "nx_cancel_operation": "Request cooperative cancellation of a running batch between child operations. Cannot interrupt a single NXOpen call; inspect final outcome.",
    "nx_batch": "Execute 1–100 add-component/placement operations serially on the NX thread under one rollback mark. Structural preflight, progress, cancellation; all-or-rollback for model changes. Supply one stable operation_id for safe retry.",
    "nx_add_component": "Add a .prt occurrence with initial translation and right-handed row-major 3x3 rotation. Work-part coordinates; p_parent=R*p_local+t. Returns typed occurrence.",
    "nx_set_component_transform": "Assign absolute translation and row-major rotation to an immediate child. Read-back verified; repeating the same placement is idempotent. Activate owning subassembly for nested placement.",
    "nx_reposition_component": "Relative translation and rotation of immediate child in work-part coordinates. Degrees, Rz*Ry*Rx. Use a stable operation_id for retry; use nx_set_component_transform for absolute placement.",
    "nx_measure_distance": "Measure minimum BREP distance for body, face, edge, feature-body or component pairs, including nested occurrences. Returns closest points, accuracy and work-part units. Zero does not prove interference.",
    "nx_list_topology": "Enumerate body topology. Optional face restricts results to that face and its boundary edges; include_adjacency returns face_edges and edge_faces ID mappings. compact retains minimal typed IDs. Use current upward-face boundary edges for flat-pattern orientation rather than guessing a former outer edge. Reacquire after edits/rollback/close.",
    "nx_rename_object": "Rename a referenced object and return its actual NX-normalized display name. Reacquire references afterward.",
    "nx_capabilities": "Inspect NX-version-specific tested scope. Use tool=exact_name or prefix=nx_sheet to limit response size; omit both for all tools. API detection is separate from native test evidence. Batch NX has no model viewport.",
}

READ_ONLY = {
    "nx_flat_pattern_orientation_edges",
    "nx_list_reference_sets",
    "nx_list_datums",
    "nx_display_info",
    "nx_list_sections",
    "nx_view_info",
    "nx_check_interference",
    "nx_check_clearance",
    "nx_ui_control",
    "nx_status",
    "nx_list_sketches",
    "nx_list_features",
    "nx_list_bodies",
    "nx_list_components",
    "nx_get_bounding_box",
    "nx_measure_volume",
    "nx_measure_distance",
    "nx_measure_angle",
    "nx_list_topology",
    "nx_checkpoint_state",
    "nx_capabilities",
    "nx_operation_status",
    "nx_workspace_info",
    "nx_read_result",
}
READ_ONLY.update(
    authoring_server.READ_ONLY
    | sheet_metal_server.READ_ONLY
    | freeform_server.READ_ONLY
    | manufacturing_server.READ_ONLY
    | assembly_documentation_server.READ_ONLY
    | documentation_editing_server.READ_ONLY
)
for _group in feature_group_modules():
    READ_ONLY.update(_group.READ_ONLY)
DESCRIPTIONS.update(
    {
        name: obj.__doc__ or name
        for name, obj in {
            **vars(authoring_server),
            **vars(sheet_metal_server),
            **vars(freeform_server),
            **vars(manufacturing_server),
            **vars(assembly_documentation_server),
            **vars(documentation_editing_server),
        }.items()
        if name.startswith("nx_") and inspect.isfunction(obj)
    }
)

SIDE = {
    "nx_workspace_info",
    "nx_read_result",
    "nx_operation_status",
    "nx_cancel_operation",
}
PATHS = {
    "nx_export_explosion_animation": "path",
    "nx_export_flat_pattern": "path",
    "nx_set_sheet_metal_defaults": "bend_table",
    "nx_render_view": "path",
    "nx_component_action": "part_path",
    "nx_save_presentation": "path",
    "nx_restore_presentation": "path",
    "nx_inspection_report": "path",
    "nx_add_component": "part_path",
    "nx_screenshot": "path",
}


class IntegrationEnvelope(BaseModel):
    """Common output contract; each tool's result fields remain extensible."""

    model_config = ConfigDict(extra="allow")
    status: Literal["success", "error"]
    warnings: list[str]
    units: str | None
    operation_id: str | None = None
    mutation_outcome: str | None = None


def envelope(payload, error=False):
    if payload.get("status") not in {None, "success", "error"}:
        payload = {**payload, "validation_status": payload["status"], "status": "success"}
    payload = {"status": "error" if error else "success", "warnings": [], "units": None, **payload}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
        isError=error,
    )


def unknown_tool_error(name, known):
    """NX_TOOL_NOT_FOUND (as on the NX side) with close-match suggestions."""
    import difflib

    matches = difflib.get_close_matches(str(name), list(known), n=3, cutoff=0.6)
    return NXToolError(
        "NX_TOOL_NOT_FOUND",
        f"Unknown tool: {name}",
        suggestion=("Did you mean " + ", ".join(matches) + "? " if matches else "")
        + "Use nx_discover_tools to find exact tool names.",
        details={"mutation_outcome": "not_started", "close_matches": matches},
    )


def tool_definitions(modules=None):
    """Collect module-level nx_* signatures; each tool name must have one definition."""
    if modules is None:
        modules = [
            sys.modules[__name__],
            authoring_server,
            sheet_metal_server,
            freeform_server,
            manufacturing_server,
            assembly_documentation_server,
            documentation_editing_server,
            *feature_group_modules(),
        ]
    definitions, owners = {}, {}
    for module in modules:
        for name, obj in vars(module).items():
            if not (name.startswith("nx_") and inspect.isfunction(obj)):
                continue
            if name in definitions and definitions[name] is not obj:
                raise RuntimeError(
                    f"NX MCP tool {name} is defined twice: {owners[name]} and {module.__name__}"
                )
            definitions[name], owners[name] = obj, module.__name__
    return definitions


def configure(mcp, bridge, workspace):
    existing = dict(mcp._tool_manager._tools)
    definitions = tool_definitions()
    if duplicates := sorted(set(existing) & set(definitions)):
        raise RuntimeError(
            "NX MCP tools defined twice (certified.py and a *_server.py signature): "
            + ", ".join(duplicates)
        )
    names = set(existing) | set(definitions)
    for name in names:
        old = existing.get(name)
        fn = definitions.get(name) or old.fn
        sig = inspect.signature(fn)
        # Resolve postponed annotations in the original callable's module.
        from typing import get_type_hints

        hints = get_type_hints(fn, include_extras=True)
        parameters = [
            p.replace(annotation=hints.get(p.name, p.annotation)) for p in sig.parameters.values()
        ]
        if name not in READ_ONLY and name not in SIDE:
            parameters.append(
                inspect.Parameter(
                    "operation_id",
                    inspect.Parameter.KEYWORD_ONLY,
                    default=None,
                    annotation=str | None,
                )
            )
        sig = sig.replace(parameters=parameters, return_annotation=CallToolResult)

        def factory(method, signature):
            async def proxy(**kwargs):
                # Errors before the request leaves the sidecar (argument binding,
                # workspace path checks) provably did not start the operation.
                forwarded = bridged = False
                try:
                    bound = signature.bind(**kwargs)
                    bound.apply_defaults()
                    from pydantic_core import to_jsonable_python

                    params = to_jsonable_python(dict(bound.arguments))
                    if "operation_id" in params and method not in {
                        "nx_operation_status",
                        "nx_cancel_operation",
                    }:
                        params["operation_id"] = params["operation_id"] or (
                            "op_" + uuid.uuid4().hex
                        )
                    if method in SIDE:
                        forwarded = True
                        result = artifact_call(method, params, workspace)
                    else:
                        if (path_key := PATHS.get(method)) and params.get(path_key) is not None:
                            params[path_key] = str(workspace.resolve(params[path_key]))
                        if method == "nx_batch":
                            for op in params["operations"]:
                                if op.get("method") == "nx_add_component" and "part_path" in op.get(
                                    "params", {}
                                ):
                                    op["params"]["part_path"] = str(
                                        workspace.resolve(op["params"]["part_path"])
                                    )
                        if intent := REQUEST_INTENT.get():
                            params["intent"] = intent
                        forwarded = bridged = True
                        result = await bridge.call(method, params)
                    response = envelope(result, error=result.get("status") == "error")
                    if method in {"nx_screenshot", "nx_render_view"} and not response.isError:
                        file = workspace.resolve(result["path"])
                        if file.stat().st_size <= 8 * 1024 * 1024:
                            data = file.read_bytes()
                            if hashlib.sha256(data).hexdigest() != result["sha256"]:
                                raise NXToolError(
                                    "NX_ARTIFACT_CHANGED",
                                    "Capture file changed after its operation committed",
                                    details={"mutation_outcome": "committed"},
                                )
                            response.content.append(
                                ImageContent(
                                    type="image",
                                    mimeType="image/png",
                                    data=base64.b64encode(data).decode(),
                                )
                            )
                        else:
                            result.setdefault("warnings", []).append(
                                "Image exceeds the inline limit; use nx_download_file"
                            )
                            response = envelope(result)
                    return response
                except Exception as e:
                    operation_id = params.get("operation_id") if "params" in locals() else None
                    if isinstance(e, NXToolError):
                        error = e
                    elif not bridged and isinstance(e, (WorkspaceViolation, ValueError, TypeError)):
                        error = NXToolError(
                            "NX_PATH_OUTSIDE_WORKSPACE"
                            if isinstance(e, WorkspaceViolation)
                            else "NX_INVALID_ARGUMENT",
                            str(e),
                        )
                    else:
                        # A sidecar fault, or anything after the request reached NX
                        # (e.g. reading a committed capture), is not an argument error.
                        error = NXToolError(
                            "NX_INTERNAL_ERROR",
                            f"Sidecar failed handling {method}: {type(e).__name__}: {e}",
                            suggestion="Query nx_operation_status before retrying"
                            if forwarded and operation_id
                            else None,
                            details={"mutation_outcome": "unknown" if forwarded else "not_started"},
                        )
                    if operation_id:
                        error.details.setdefault("operation_id", operation_id)
                        error.details.setdefault(
                            "mutation_outcome", "unknown" if forwarded else "not_started"
                        )
                    if error.details.get("mutation_outcome") == "unknown":
                        # The operation may have run; a blind retry could repeat it.
                        error.retryable = False
                    return envelope(error.as_dict(), True)

            proxy.__name__ = method
            proxy.__signature__ = signature
            return proxy

        if old:
            mcp.remove_tool(name)
        description = DESCRIPTIONS.get(
            name,
            (inspect.getdoc(definitions[name]) if name in definitions else None)
            or (old.description if old else name),
        )
        description = description.replace("EXPERIMENTAL: ", "")
        if description.strip() == name:
            description = (
                "Experimental NX operation; semantics and installed API support have not been validated. "
                + name
            )
        mcp.add_tool(
            factory(name, sig),
            name=name,
            description=description,
            structured_output=False,
            annotations=ToolAnnotations(
                readOnlyHint=name in READ_ONLY and name != "nx_ui_control",
                idempotentHint=name in READ_ONLY
                or name
                in {
                    "nx_set_component_transform",
                    "nx_edit_explosion",
                    "nx_show_explosion",
                },
            ),
        )
        tool = mcp._tool_manager.get_tool(name)
        tool.fn_metadata.output_model = IntegrationEnvelope
        tool.fn_metadata.output_schema = output_schema(
            name, IntegrationEnvelope.model_json_schema()
        )
        tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
        tool.fn_metadata.arg_model.model_rebuild(force=True)
        tool.parameters = tool.fn_metadata.arg_model.model_json_schema()
        if name == "nx_edit_explosion":
            for schema in tool.parameters["properties"]["placements"]["anyOf"]:
                if schema.get("type") == "array":
                    schema.update(maxItems=1000, items=authoring_server.EXPLOSION_PLACEMENT_SCHEMA)
    original_call = mcp.call_tool

    async def uniform_call(name, arguments):
        try:
            return await original_call(name, arguments)
        except Exception as error:
            from pydantic import ValidationError

            if mcp._tool_manager.get_tool(name) is None:
                failure = unknown_tool_error(name, mcp._tool_manager._tools)
            elif isinstance(error.__cause__, ValidationError):
                # FastMCP validates arguments before the proxy runs: nothing was sent.
                failure = NXToolError(
                    "NX_INVALID_ARGUMENT", str(error), details={"mutation_outcome": "not_started"}
                )
            else:
                # The proxy maps its own failures; anything else escaped after the
                # tool started, so the outcome is unknown.
                failure = NXToolError(
                    "NX_INTERNAL_ERROR",
                    str(error),
                    details={"mutation_outcome": "unknown"},
                )
                if isinstance(arguments, dict) and arguments.get("operation_id"):
                    failure.details["operation_id"] = arguments["operation_id"]
            return envelope(failure.as_dict(), True)

    mcp.call_tool = uniform_call
    mcp._mcp_server.call_tool(validate_input=False)(uniform_call)
    mcp._mcp_server.instructions = "Siemens NX v2606 integration. Use nx_capabilities for tested scope. Discover the host root with nx_workspace_info. File paths are workspace-relative or absolute inside that root; use explicit project subfolders on every file call. Use client-supplied operation_id for mutation retry; query receipts after transport failure. No general certification is claimed."


def artifact_call(method, p, workspace):
    store = OperationStore(workspace.root)
    if method == "nx_read_result":
        from nx_mcp.result_transport import read_result

        return read_result(workspace.root / ".nx-mcp" / "bridge-results", **p)
    if method == "nx_operation_status":
        return store.get(p["operation_id"])
    if method == "nx_cancel_operation":
        record = store.get(p["operation_id"])
        if record["state"] != "running" or record.get("method") != "nx_batch":
            raise NXToolError("NX_NOT_CANCELLABLE", "Only running batches accept cancellation")
        store.path(p["operation_id"]).with_suffix(".cancel").touch()
        return {
            "status": "success",
            "operation_id": p["operation_id"],
            "cancellation_requested": True,
        }
    if method == "nx_workspace_info":
        return {
            "status": "success",
            "root": str(workspace.root),
            "path_host": "NX server",
            "relative_to": str(workspace.root),
            "absolute_paths": "accepted inside workspace only",
            "parent_creation": ["nx_create_part", "nx_save_part", "nx_upload_file"],
            "current_directory": "No mutable current directory; use explicit paths on every call",
        }
    raise NXToolError("NX_TOOL_NOT_FOUND", f"{method} is not a sidecar tool")

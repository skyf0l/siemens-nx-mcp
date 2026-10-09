"""Uniform MCP envelopes and workspace-scoped artifacts for the NX 2606 bridge."""

from __future__ import annotations

import inspect
import json
import sys
import uuid
from contextvars import ContextVar
from typing import Annotated, Literal

from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from nx_mcp import (
    assembly_documentation_server,
    authoring_server,
    documentation_editing_server,
    freeform_server,
    manufacturing_server,
)
from nx_mcp.feature_groups import server_modules as feature_group_modules
from nx_mcp.output_schemas import output_schema
from nx_mcp.recovery import OperationStore
from nx_mcp.runtime import NXToolError
from nx_mcp.workspace import WorkspaceViolation

# The agent's short note on why it makes a call (set by nx_invoke). It travels to
# the NX control panel with the bridge request and is never a handler argument.
REQUEST_INTENT: ContextVar[str | None] = ContextVar("nx_mcp_request_intent", default=None)


def nx_list_datums():
    """List work-part datum planes, axes and coordinate systems with IDs and blanked state. Does not recurse into component prototypes."""


# Public schemas for tools whose NX handlers live in hardened.py
# and nx_bridge.py. Descriptions come from DESCRIPTIONS
# where one exists.
# Signature-only definitions are used to publish the actual bridge arguments.
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


def nx_read_result(
    result_id: str,
    field: str = "",
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
):
    """Read a stored oversized bridge result without repeating its operation. field is a JSON pointer; arrays/strings are paged. Nested omissions remain explicit. Snapshot expiry does not delete operation receipts."""


def nx_capabilities(tool: str | None = None, prefix: str | None = None):
    pass


def nx_rename_object(object_id: str, name: str):
    pass


def nx_workspace_info():
    pass


DESCRIPTIONS = {
    "nx_workspace_info": "Discover the NX host workspace root and path rules. Paths refer to the NX machine, not the MCP client's filesystem. No session-wide current directory is changed.",
    "nx_checkpoint": "Create an in-session model undo checkpoint. NX v2606 saves expire native marks; create a new checkpoint after save. Restart/close also invalidates checkpoints.",
    "nx_checkpoint_state": "Inspect available checkpoint IDs and retained model-operation history. Read-only calls retain marks. Native NX save can expire them; availability is checked against NX.",
    "nx_rollback": "Rollback to an in-session checkpoint. Rejects rollback across mutations to unrelated parts. Reacquire object IDs afterward; save explicitly to persist.",
    "nx_operation_status": "Read durable request state without waiting on the NX thread: running, committed, failed, unknown. Unknown never authorizes blind retry.",
    "nx_cancel_operation": "Request cooperative cancellation of a running batch between child operations. Cannot interrupt a single NXOpen call; inspect final outcome.",
    "nx_list_topology": "Enumerate body topology. Optional face restricts results to that face and its boundary edges; include_adjacency returns face_edges and edge_faces ID mappings. compact retains minimal typed IDs. Use current upward-face boundary edges for flat-pattern orientation rather than guessing a former outer edge. Reacquire after edits/rollback/close.",
    "nx_rename_object": "Rename a referenced object and return its actual NX-normalized display name. Reacquire references afterward.",
    "nx_capabilities": "Inspect NX-version-specific tested scope. Use tool=exact_name or prefix=nx_sheet to limit response size; omit both for all tools. API detection is separate from native test evidence. Batch NX has no model viewport.",
}

READ_ONLY = {
    "nx_list_datums",
    "nx_status",
    "nx_list_sketches",
    "nx_list_features",
    "nx_list_bodies",
    "nx_list_topology",
    "nx_checkpoint_state",
    "nx_capabilities",
    "nx_operation_status",
    "nx_workspace_info",
    "nx_read_result",
}
READ_ONLY.update(
    authoring_server.READ_ONLY
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
PATHS: dict[str, str] = {}


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
                        if intent := REQUEST_INTENT.get():
                            params["intent"] = intent
                        forwarded = bridged = True
                        result = await bridge.call(method, params)
                    return envelope(result, error=result.get("status") == "error")
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
                readOnlyHint=name in READ_ONLY,
                idempotentHint=name in READ_ONLY,
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
        if record["state"] != "running" or record.get("method") != "nx_run_batch":
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

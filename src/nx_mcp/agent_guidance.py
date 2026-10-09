"""Conservative task guidance, with unknown effects explicit rather than guessed."""

from nx_mcp.manifest import load_manifest

MANIFEST = load_manifest()

# Reviewed contracts; omitted tools retain their descriptions and unknown effects.
GUIDANCE: dict[str, tuple] = {}
GEOMETRY: set[str] = set()
VISIBILITY: set[str] = set()
SAVES: set[str] = set()
FILES: set[str] = set(SAVES)
INVALIDATES: set[str] = set()


def guidance(tool):
    readonly = bool(tool.annotations and tool.annotations.readOnlyHint)
    reviewed = GUIDANCE.get(tool.name)

    def effect(group):
        return True if tool.name in group else (False if readonly else None)

    return {
        "prerequisites": reviewed[0]
        if reviewed
        else [
            "Inspect tool description and exact schema; additional native prerequisites may apply"
        ],
        "supported_object_kinds": reviewed[1] if reviewed else None,
        "minimal_example": reviewed[2] if reviewed else None,
        "example_convention": "Angle-bracket IDs are placeholders; use live references. Add a unique operation_id for mutations.",
        "native_evidence": MANIFEST["tools"].get(
            tool.name, {"status": "unavailable", "scope": "No manifest entry"}
        ),
        "effects": {
            "read_only": readonly,
            "geometry_or_assembly": effect(GEOMETRY),
            "visibility_or_view": effect(VISIBILITY),
            "saves_part": effect(SAVES),
            "writes_files": effect(FILES),
            "invalidates_references": effect(INVALIDATES),
            "expires_undo_checkpoints": effect(SAVES),
            "recovery": "Saves and part lifecycle changes can expire native undo marks. Inspect nx_list_checkpoints and create a new checkpoint after these operations; durable operation receipts remain available.",
            "unknown_semantics": "null means not reviewed; true can be conditional on arguments. Read the exact tool description.",
        },
    }


def next_actions(method, payload):
    """Only suggest actions with already returned IDs, never new mutations."""
    actions = []

    def identity(value):
        return value.get("id") if isinstance(value, dict) else None

    body = next((identity(x) for x in payload.get("bodies", []) if identity(x)), None)
    body = body or identity(payload.get("body"))
    obj = payload.get("object", {})
    if isinstance(obj, dict) and obj.get("kind") == "body":
        body = body or identity(obj)
    if body:
        actions.append(
            {
                "tool": "nx_list_objects",
                "arguments": {"kind": "face", "owner": body},
                "purpose": "Inspect current faces and edges before selecting geometry",
            }
        )
    if isinstance(obj, dict) and obj.get("kind") == "sketch":
        actions.append(
            {
                "tool": "nx_get_sketch",
                "arguments": {"sketch": obj["id"], "include": ["status"]},
                "purpose": "Check sketch constraints and the solver status",
            }
        )
    if payload.get("path") and payload.get("sha256"):
        actions.append(
            {
                "tool": "nx_download_file",
                "arguments": {"path": payload["path"], "delivery": "metadata"},
                "purpose": "Retrieve artifact metadata before programmatic download",
            }
        )
    return actions

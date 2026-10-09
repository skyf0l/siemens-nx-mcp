"""JSON Schema payload contracts for the integration surface.

Schemas describe committed results; they do not run a second validation step after
an NX mutation. Errors have a separate branch so success cardinality requirements
never hide a useful kernel error. Additive native metadata remains permitted.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

S = {"type": "string"}
N = {"type": "number"}
COUNT = {"type": "integer", "minimum": 0}
B = {"type": "boolean"}
NULL = {"type": "null"}
OUTCOMES = [
    "not_started",
    "running",
    "committed",
    "rolled_back",
    "partial",
    "unknown",
    "not_applicable",
]


def arr(item: dict, count: int | None = None) -> dict:
    result: dict[str, Any] = {"type": "array", "items": item}
    if count is not None:
        result.update(minItems=count, maxItems=count)
    return result


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": True,
    }


VEC = arr(N, 3)
REF = obj(
    {
        "id": S,
        "kind": S,
        "name": S,
        "part_id": S,
        "session_id": S,
        "generation_id": S,
        "owner_part_path": S,
        "journal_id": S,
        "display_name": S,
    },
    ["id", "kind", "name", "part_id"],
)
REF["description"] = (
    "Opaque identity, separate from display name. Reacquire after close, rollback or manual handoff; use occurrence context for assemblies."
)
HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
META = {"path": S, "size": COUNT, "sha256": HASH}

# Required fields follow actual bridge implementations; optional metadata remains typed.
PAYLOADS: dict[str, dict[str, Any]] = {
    "nx_get_bounding_box": obj(
        {
            "min": VEC,
            "max": VEC,
            "dimensions": VEC,
            "coordinate_frame": S,
            "bounds_type": {"enum": ["exact", "conservative"]},
            "body_count": COUNT,
            "scope": S,
            "bodies": arr(obj({"body": REF, "box": arr(N, 6), "solid": B})),
        }
    ),
    "nx_measure_volume": obj(
        {
            "volume_mm3": N,
            "volume_units": {"const": "mm^3"},
            "semantics": {"const": "sum_of_included_bodies"},
            "body_count": COUNT,
            "scope": S,
            "bodies": arr(obj({"body": REF, "volume_mm3": N})),
        }
    ),
    "nx_list_topology": obj({"body": REF, "faces": arr(REF), "edges": arr(REF), "solid": B}),
    "nx_operation_status": obj(
        {
            "operation_id": S,
            "state": {"enum": ["running", "committed", "failed", "unknown", "rolled_back"]},
            "mutation_outcome": {"enum": OUTCOMES},
            "method": S,
            "reason": S,
            "session_id": S,
            "started_at": S,
            "finished_at": S,
            "result": {"type": "object"},
            "error": {"type": "object"},
            "reverted_by": S,
            "query_operation_id": S,
            "query_session_id": S,
        },
        ["operation_id", "state", "mutation_outcome"],
    ),
    "nx_checkpoint": obj({"checkpoint_id": S, "message": S}),
    "nx_rollback": obj({"checkpoint_id": S, "message": S}),
    "nx_checkpoint_state": obj(
        {
            "checkpoints": arr(
                obj({"checkpoint_id": S, "part_id": S, "index": COUNT, "label": S, "available": B})
            ),
            "undo_depth": COUNT,
            "save_semantics": S,
            "retention": S,
        }
    ),
}

# Lifecycle and authoring results use their actual native field names.
INTEGER = {"type": "integer"}
PAGE = {
    "count": COUNT,
    "total_count": COUNT,
    "offset": COUNT,
    "next_offset": {"anyOf": [COUNT, NULL]},
}
EXPRESSION = obj(
    {
        "object": REF,
        "name": S,
        "formula": S,
        "type": S,
        "value": {"anyOf": [N, NULL]},
        "units": S,
        "editable": B,
        "parents": arr(REF),
        "dependents": arr(REF),
        "dependency_scope": S,
    }
)
PAYLOADS.update(
    {
        "nx_list_drawings": obj(
            {
                "sheets": arr(
                    obj(
                        {
                            "object": REF,
                            "name": S,
                            "active": B,
                            "width": N,
                            "height": N,
                            "units": S,
                            "scale": arr(N, 2),
                            "views": arr(obj({"object": REF, "name": S})),
                        }
                    )
                ),
                "units": {"const": "per_sheet"},
                "coordinate_frame": {"const": "drawing_sheet"},
                "modeling_active": B,
            }
        ),
    }
)


REFERENCE_SET = obj(
    {
        "object": REF,
        "name": S,
        "member_count": COUNT,
        "members": arr(REF),
        "add_components_automatically": B,
    }
)
PAYLOADS.update(
    {
        "nx_list_datums": obj(
            {
                "datums": arr(obj({"object": REF, "native_type": S, "blanked": B})),
                "count": COUNT,
                "scope": {"const": "work_part"},
            }
        ),
        "nx_list_dimensions": obj(
            {
                "dimensions": arr(
                    obj(
                        {
                            "object": REF,
                            "native_type": S,
                            "computed_value": N,
                            "retained": B,
                            "measurement_valid": B,
                            "origin": VEC,
                        }
                    )
                ),
                "coordinate_frame": S,
            }
        ),
    }
)


PAYLOADS.update(
    {
        "nx_read_result": obj(
            {
                "result_id": S,
                "field": S,
                "value": {},
                "total_count": {"anyOf": [COUNT, NULL]},
                "offset": COUNT,
                "next_offset": {"anyOf": [COUNT, NULL]},
                "omitted": {"type": "object"},
            }
        ),
    }
)


def output_schema(name: str, common: dict) -> dict:
    """Keep an object root for MCP; discriminate errors before success payloads."""
    schema = deepcopy(common)
    schema["title"] = name + " result"
    schema["description"] = "Structured success/error envelope with additive metadata. " + (
        "Tool-specific payload is typed."
        if name in PAYLOADS
        else "Tool-specific payload remains extensible."
    )
    schema["properties"].update(
        {
            "full_result": obj(
                {
                    "id": S,
                    "sha256": S,
                    "size": COUNT,
                    "tool": {"const": "nx_read_result"},
                    "immutable": {"const": True},
                }
            ),
            "omitted": {"type": "object"},
            "code": S,
            "message": S,
            "retryable": B,
            "suggestion": S,
            "nx_code": {"type": ["integer", "string"]},
            "details": obj({"operation_id": S, "mutation_outcome": {"enum": OUTCOMES}}, []),
        }
    )
    schema["properties"]["mutation_outcome"] = {"anyOf": [{"enum": OUTCOMES}, NULL]}
    schema["allOf"] = [
        {
            "if": {"properties": {"status": {"const": "error"}}},
            "then": {"required": ["code", "message", "retryable"]},
        }
    ]
    if name in PAYLOADS:
        schema["allOf"].append(
            {
                "if": {
                    "properties": {"status": {"const": "success"}},
                    "not": {"required": ["full_result"]},
                },
                "then": deepcopy(PAYLOADS[name]),
            }
        )
    return schema

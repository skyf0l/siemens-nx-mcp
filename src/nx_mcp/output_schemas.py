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
PAYLOADS: dict[str, dict[str, Any]] = {}


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
                    "tool": {"const": "nx_get_result"},
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

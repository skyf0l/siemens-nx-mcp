"""Standard-library-only types used inside the NX embedded Python runtime."""

from __future__ import annotations

import traceback
from dataclasses import asdict, dataclass
from typing import Literal

# One error hierarchy for legacy and catalogue code: NXToolError is defined in
# nx_mcp.core.errors (stdlib only) and core.errors.NXError subclasses it.
from nx_mcp.core.errors import NXToolError

ObjectKind = Literal[
    "reference_set",
    "datum",
    "part",
    "sketch",
    "curve",
    "point",
    "feature",
    "body",
    "component",
    "face",
    "edge",
    "section",
    "constraint",
    "expression",
    "component_pattern",
    "assembly_constraint",
    "drawing_sheet",
    "drawing_view",
    "dimension",
    "annotation",
    "traceline",
    "explosion",
    "modeling_view",
]


@dataclass(frozen=True)
class ObjectRef:
    id: str
    kind: ObjectKind
    name: str
    part_id: str

    def model_dump(self) -> dict[str, str]:
        """Match the sidecar model interface without importing Pydantic in NX."""
        return asdict(self)


# Native license checkout failures (NX 2406: 948801 missing token feature,
# 949895 license feature not available). The tool works where the seat has it.
LICENSE_ERROR_CODES = frozenset({948801, 949895})


def api_error(error: BaseException) -> NXToolError:
    """Wrap an unexpected exception as NX_API_ERROR (NX_LICENSE_UNAVAILABLE for a
    native license checkout failure), keeping where it was raised.

    The short traceback tail makes environment faults (a missing module or an
    unimported NXOpen namespace) distinguishable from genuine NX API failures.
    """
    frames = traceback.format_exception(type(error), error, error.__traceback__)
    code = getattr(error, "ErrorCode", None)
    licensed = code in LICENSE_ERROR_CODES
    return NXToolError(
        "NX_LICENSE_UNAVAILABLE" if licensed else "NX_API_ERROR",
        "This NX seat has no license for the NX command this tool uses: " + str(error)
        if licensed
        else str(error),
        nx_code=code,
        details={
            "exception_type": type(error).__name__,
            "traceback": "".join(frames[-6:])[-4000:],
        },
    )

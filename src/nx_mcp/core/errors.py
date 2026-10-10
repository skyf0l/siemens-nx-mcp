"""Error taxonomy (tool-surface.md 1.8): 22 categories, 67 codes, ``native()``.

Handlers raise only through :func:`fail` or :func:`native`. The kernel turns any
:class:`NXError` into ``{category, code, message, hint, field, retry, nx_code,
details}``; ``retry`` comes from the category table unless ``fail(retry=...)``
overrides it for one error (a refusal no fix can turn into a success).
"""

from __future__ import annotations

import contextlib
import os
import traceback
from typing import Any, NoReturn

# category -> (retry, codes, default hint)
CATEGORIES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "invalid_argument": (
        "after_fix",
        ("NX_INVALID_ARGUMENT", "NX_INVALID_FORMULA"),
        "Fix the argument named in field; the same call never succeeds",
    ),
    "unsupported": (
        "never",
        (
            "NX_UNSUPPORTED_OPTION",
            "NX_UNSUPPORTED_GEOMETRY",
            "NX_UNSUPPORTED_FILE_TYPE",
            "NX_NOT_RUNNING",  # the operation already finished: nothing to cancel or wait for
        ),
        "Choose another tool or approach",
    ),
    "not_found": (
        "after_fix",
        ("NX_NOT_FOUND", "NX_FILE_NOT_FOUND", "NX_RESULT_EXPIRED", "NX_UNKNOWN_TOOL"),
        "Re-list the objects or files and use a current reference or path",
    ),
    "stale_reference": (
        "after_reacquire",
        ("NX_STALE_REFERENCE", "NX_STALE_CHECKPOINT", "NX_STALE_PREVIEW", "NX_STALE_SOURCE"),
        "Reacquire references with nx_list_objects, nx_find_geometry or nx_resolve_reference",
    ),
    "wrong_kind": (
        "after_fix",
        ("NX_WRONG_KIND", "NX_WRONG_GEOMETRY"),
        "Pass a reference of the kind or geometry the field names",
    ),
    "wrong_owner": (
        "after_fix",
        ("NX_WRONG_OWNER", "NX_WRONG_PART_CONTEXT"),
        "Use geometry of the right body or part, or activate the owning part",
    ),
    "ambiguous": (
        "after_fix",
        ("NX_AMBIGUOUS_REFERENCE", "NX_TARGET_REQUIRED"),
        "Pass the explicit reference; details.candidates lists the choices",
    ),
    "precondition": (
        "after_fix",
        (
            "NX_SKETCH_ACTIVE",
            "NX_SKETCH_NOT_ACTIVE",
            "NX_NO_WORK_PART",
            "NX_WRONG_APPLICATION",
            "NX_NOT_LOADED",
            "NX_UNSAVED_PART",
            "NX_PART_IN_USE",
        ),
        "Run the tool named in the hint, then retry",
    ),
    "already_exists": (
        "after_fix",
        ("NX_ALREADY_EXISTS", "NX_NAME_CONFLICT"),
        "Choose another name or path, or use the existing object in details",
    ),
    "blocked": (
        "after_fix",
        ("NX_DEPENDENCY_ORDER", "NX_IN_USE", "NX_READ_ONLY", "NX_CROSS_PART"),
        "Resolve the blocker listed in details; ask the engineer when it is their data",
    ),
    "geometry_failed": (
        "after_fix",
        ("NX_BUILD_FAILED", "NX_UPDATE_FAILED", "NX_INCOMPLETE_RESULT"),
        "The model was rolled back; read the message for NX's reason and change the input "
        "it names; do not repeat unchanged",
    ),
    "solver_failed": (
        "after_fix",
        ("NX_CONSTRAINT_UNSATISFIED", "NX_SKETCH_OVERCONSTRAINED"),
        "Diagnose with nx_check_sketch and remove the conflicting constraint",
    ),
    "verification_failed": (
        "ask_engineer",
        ("NX_VERIFICATION_FAILED",),
        "NX built something different from the request and it was rolled back; report it",
    ),
    "limit_exceeded": (
        "after_fix",
        ("NX_TOO_MANY_OBJECTS", "NX_REQUEST_TOO_LARGE", "NX_RESPONSE_TOO_LARGE"),
        "Split the request or page smaller",
    ),
    "path_rejected": (
        "after_fix",
        ("NX_PATH_OUTSIDE_WORKSPACE", "NX_PATH_RESERVED"),
        "Use a workspace-relative path (nx_get_status shows the root)",
    ),
    "file_failed": (
        "after_fix",
        (
            "NX_IMPORT_FAILED",
            "NX_EXPORT_FAILED",
            "NX_SAVE_FAILED",
            "NX_CAPTURE_FAILED",
            "NX_TRANSFER_FAILED",
        ),
        "Read details, fix the input; ask the engineer about save failures",
    ),
    "unavailable": (
        "never",
        (
            "NX_LICENSE_UNAVAILABLE",
            "NX_VERSION_UNSUPPORTED",
            "NX_VIEWPORT_UNAVAILABLE",
            "NX_CATALOG_UNAVAILABLE",
        ),
        "Tell the engineer which license or build is missing; use an alternative",
    ),
    "engineer_control": (
        "ask_engineer",
        (
            "NX_CONTROL_DECLINED",
            "NX_CONTROL_PAUSED",
            "NX_UI_BUSY",
            "NX_PART_NOT_GRANTED",
            "NX_MODAL_DIALOG_OPEN",
        ),
        "The engineer has NX; ask in chat before retrying",
    ),
    "bridge": (
        "after_wait",
        ("NX_BRIDGE_UNAVAILABLE", "NX_PROTOCOL_MISMATCH", "NX_AUTH_FAILED"),
        "NX or the bridge is not running; check nx_get_operation before retrying a mutation",
    ),
    "interrupted": (
        "after_status_check",
        ("NX_CANCELLED", "NX_OUTCOME_UNKNOWN", "NX_IDEMPOTENCY_CONFLICT"),
        "Query nx_get_operation(operation_id); never retry a mutation blindly",
    ),
    "rollback_failed": (
        "ask_engineer",
        ("NX_ROLLBACK_FAILED", "NX_RESTORE_FAILED"),
        "The model may be partly changed: stop mutating, capture a view, tell the engineer",
    ),
    "nx_error": (
        "ask_engineer",
        ("NX_ERROR",),
        "Unclassified NX failure; retry once only after changing inputs",
    ),
}

CODES: dict[str, str] = {code: cat for cat, (_r, codes, _h) in CATEGORIES.items() for code in codes}
# Warning codes (``tx.warn`` maps any other code to NX_UPDATE_WARNING):
# NX_UPDATE_WARNING  NX reported a model update problem the call survived
# NX_APPROXIMATED    the result approximates the request (tolerance, fit)
# NX_PARTIAL_PAGE    a listing was cut short; page on
# NX_MODAL_DIALOG    NX showed a modal dialog during the call
# NX_VIEW_OVERLAP    drawing views overlap on the sheet
# NX_NO_EFFECT       the call succeeded but changed nothing visible yet (a next step is needed)
# NX_NOTE            an informational note, not a model problem (NX default density, ...)
WARNING_CODES = frozenset(
    {
        "NX_UPDATE_WARNING",
        "NX_APPROXIMATED",
        "NX_PARTIAL_PAGE",
        "NX_MODAL_DIALOG",
        "NX_VIEW_OVERLAP",
        "NX_NO_EFFECT",
        "NX_NOTE",
    }
)
RETRY_VALUES = frozenset(
    {"never", "after_fix", "after_wait", "after_reacquire", "after_status_check", "ask_engineer"}
)

# Native license checkout failures (NX 2406: 948801 missing token feature,
# 949895 license feature not available).
LICENSE_ERROR_CODES = frozenset({948801, 949895})
# "Update undo happened.": an update failed and NX undid it, as the host's update
# failure action (host/quiet.py) asks instead of opening "Error During Update".
UPDATE_UNDONE_CODE = 670024


def dev_mode() -> bool:
    return os.environ.get("NX_MCP_DEV") == "1"


class NXError(Exception):
    """A classified failure: a stable ``code`` from the table, its ``category``,
    ``hint``, ``field``, ``nx_code`` and ``details``.

    Tool handlers create it with :func:`fail` or :func:`native`; the host and the
    bridge raise it directly. :meth:`error_dict` is the envelope ``error`` object,
    :meth:`as_dict` the bridge wire payload. ``retryable`` (wire only) follows the
    category unless the bridge states it.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        hint: str | None = None,
        nx_code: int | None = None,
        details: dict | None = None,
        retryable: bool | None = None,
        retry: str | None = None,
    ) -> None:
        if code not in CODES:
            # A handler raised an unlisted code; the contract test reports it.
            details = {**(details or {}), "unlisted_code": code}
            code = "NX_ERROR"
        super().__init__(str(message))
        self.code = code
        self.message = str(message)
        self.category = CODES[code]
        self.field = field
        self.hint = hint
        self.nx_code = nx_code
        self.details: dict[str, Any] = dict(details or {})
        # Development traceback (NX_MCP_DEV=1): never in the envelope; the kernel writes
        # it to the operation receipt and the workspace traceback log.
        self.trace: str | None = None
        self._retry = retry if retry in RETRY_VALUES else None
        self.retryable = (
            self.retry in ("after_wait", "after_reacquire") if retryable is None else retryable
        )

    @property
    def retry(self) -> str:
        """The category's retry, or the override ``fail(retry=...)`` gave this error."""
        return self._retry or CATEGORIES[self.category][0]

    def as_dict(self) -> dict:
        """The bridge wire payload (``BridgeClient`` rebuilds the error from it)."""
        out: dict[str, Any] = {
            "status": "error",
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.hint:
            out["suggestion"] = self.hint
        if self.nx_code is not None:
            out["nx_code"] = self.nx_code
        if self.details:
            out["details"] = self.details
        out["category"] = self.category
        if self.field:
            out["field"] = self.field
        if self._retry:
            out["retry"] = self._retry
        return out

    def error_dict(self) -> dict:
        """The surface 1.8 ``error`` object of an envelope."""
        return {
            "category": self.category,
            "code": self.code,
            "message": self.message,
            "hint": self.hint or CATEGORIES[self.category][2],
            "field": self.field,
            "retry": self.retry,
            "nx_code": self.nx_code,
            "details": self.details,
        }


def fail(
    code: str,
    message: str,
    *,
    field: str | None = None,
    hint: str | None = None,
    details: dict | None = None,
    nx_code: int | None = None,
    retry: str | None = None,
) -> NoReturn:
    """Raise :class:`NXError` with a stable code from the table (``nx_code`` kept).

    ``retry`` overrides the category's retry for this error only, e.g. ``"never"`` for
    a refusal that no fix of the same call can turn into a success."""
    if retry is not None and retry not in RETRY_VALUES:
        raise ValueError(f"retry must be one of {sorted(RETRY_VALUES)}")
    raise NXError(
        code, message, field=field, hint=hint, nx_code=nx_code, details=details, retry=retry
    )


def _native_code(error: BaseException) -> int | None:
    code = getattr(error, "ErrorCode", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def classify(error: BaseException, codes: dict | None = None, default: str = "NX_ERROR") -> NXError:
    """The :class:`NXError` for any exception (see ``native()`` for the rules)."""
    if isinstance(error, NXError):
        return error
    nx_code = _native_code(error)
    details: dict = {"exception_type": type(error).__name__}
    trace = None
    if dev_mode():
        frames = traceback.format_exception(type(error), error, error.__traceback__)
        trace = "".join(frames[-6:])[-4000:]
    classified = _classify(error, nx_code, details, codes, default)
    # The stack (local paths) never reaches the agent, not even with NX_MCP_DEV=1: NX's
    # message, nx_code and the exception type say what happened; the kernel writes the
    # traceback to the operation receipt and the workspace traceback log.
    classified.trace = trace
    return classified


def _classify(error, nx_code, details, codes, default) -> NXError:
    if nx_code in LICENSE_ERROR_CODES:
        return NXError(
            "NX_LICENSE_UNAVAILABLE",
            "This NX seat has no license for the NX command this tool uses: " + str(error),
            nx_code=nx_code,
            details=details,
        )
    if nx_code == UPDATE_UNDONE_CODE:
        return NXError(
            "NX_UPDATE_FAILED",
            "The NX model update failed and NX undid it: " + str(error),
            nx_code=nx_code,
            details=details,
        )
    if nx_code is not None and codes and nx_code in codes:
        return NXError(codes[nx_code], str(error), nx_code=nx_code, details=details)
    if nx_code is None:
        # A Python exception, not a native NX one.
        return NXError("NX_ERROR", f"{type(error).__name__}: {error}", details=details)
    return NXError(default, str(error), nx_code=nx_code, details=details)


@contextlib.contextmanager
def native(codes: dict | None = None, default: str = "NX_ERROR"):
    """Map native NX exceptions raised inside the block to stable codes.

    ``ErrorCode`` 948801/949895 -> ``NX_LICENSE_UNAVAILABLE``; 670024 (an update
    failure NX undid) -> ``NX_UPDATE_FAILED``; a code listed in
    ``codes`` (``{nx_code: "NX_..."}``) -> that code; any other native error ->
    ``default``; a plain Python exception -> ``NX_ERROR`` with its type name.
    ``nx_code`` and the native message are always kept; with ``NX_MCP_DEV=1`` the
    traceback is kept out of the envelope (``NXError.trace``: the operation receipt and
    ``.nx-mcp/logs/tracebacks.log``).
    """
    try:
        yield
    except NXError:
        raise
    except Exception as error:
        raise classify(error, codes, default) from error

"""Error taxonomy (tool-surface.md 1.8): 22 categories, 66 codes, ``native()``.

Handlers raise only through :func:`fail` or :func:`native`. The kernel turns any
:class:`NXError` into ``{category, code, message, hint, field, retry, nx_code,
details}``; ``retry`` comes from the category table.
"""

from __future__ import annotations

import contextlib
import os
import traceback
from typing import Any

# category -> (retry, codes, default hint)
CATEGORIES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "invalid_argument": (
        "after_fix",
        ("NX_INVALID_ARGUMENT", "NX_INVALID_FORMULA"),
        "Fix the argument named in field; the same call never succeeds",
    ),
    "unsupported": (
        "never",
        ("NX_UNSUPPORTED_OPTION", "NX_UNSUPPORTED_GEOMETRY", "NX_UNSUPPORTED_FILE_TYPE"),
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
        "The model was rolled back; change dimensions or inputs, do not repeat unchanged",
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
        "The engineer has NX; ask in chat before requesting control again",
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
WARNING_CODES = frozenset(
    {"NX_UPDATE_WARNING", "NX_APPROXIMATED", "NX_PARTIAL_PAGE", "NX_MODAL_DIALOG"}
)

# Native license checkout failures (NX 2406: 948801 missing token feature,
# 949895 license feature not available).
LICENSE_ERROR_CODES = frozenset({948801, 949895})
# "Update undo happened.": an update failed and NX undid it, as the host's update
# failure action (host/quiet.py) asks instead of opening "Error During Update".
UPDATE_UNDONE_CODE = 670024

# Today's codes (raised by legacy services the kernel still calls) -> new code.
LEGACY_CODES: dict[str, str] = {
    "NX_INVALID_REQUEST": "NX_INVALID_ARGUMENT",
    "NX_INVALID_STEP": "NX_INVALID_ARGUMENT",
    "NX_CATALOG_INVALID": "NX_INVALID_ARGUMENT",
    "NX_UNSUPPORTED_ARGUMENT": "NX_UNSUPPORTED_OPTION",
    "NX_UNSUPPORTED_EDIT": "NX_UNSUPPORTED_OPTION",
    "NX_UNSUPPORTED_SCOPE": "NX_UNSUPPORTED_OPTION",
    "NX_UNSUPPORTED_UNITS": "NX_UNSUPPORTED_OPTION",
    "NX_UNSUPPORTED_FRAME": "NX_UNSUPPORTED_OPTION",
    "NX_UNSUPPORTED_IMAGE": "NX_UNSUPPORTED_FILE_TYPE",
    "NX_NOT_IMPLEMENTED": "NX_UNSUPPORTED_OPTION",
    "NX_OBJECT_NOT_FOUND": "NX_NOT_FOUND",
    "NX_DIRECTORY_NOT_FOUND": "NX_FILE_NOT_FOUND",
    "NX_TOOL_NOT_FOUND": "NX_UNKNOWN_TOOL",
    "NX_NO_TABLE_HEADER": "NX_NOT_FOUND",
    "NX_OBJECT_STALE": "NX_STALE_REFERENCE",
    "NX_SAVED_REFERENCE_STALE": "NX_STALE_REFERENCE",
    "NX_STALE_TRACE_ANCHOR": "NX_STALE_REFERENCE",
    "NX_STALE_ANNOTATION_SOURCE": "NX_STALE_SOURCE",
    "NX_DISPLAY_SNAPSHOT_STALE": "NX_STALE_REFERENCE",
    "NX_CHECKPOINT_STALE": "NX_STALE_CHECKPOINT",
    "NX_PREVIEW_STALE": "NX_STALE_PREVIEW",
    "NX_SOURCE_CHANGED": "NX_STALE_SOURCE",
    "NX_ARTIFACT_CHANGED": "NX_STALE_SOURCE",
    "NX_EXPLOSION_ASSEMBLY_CHANGED": "NX_STALE_SOURCE",
    "NX_OBJECT_TYPE_MISMATCH": "NX_WRONG_KIND",
    "NX_NOT_SOLID": "NX_WRONG_GEOMETRY",
    "NX_NOT_SHEET": "NX_WRONG_GEOMETRY",
    "NX_NON_PLANAR": "NX_WRONG_GEOMETRY",
    "NX_NOT_SHEET_METAL": "NX_WRONG_GEOMETRY",
    "NX_NOT_DIRECTORY": "NX_WRONG_KIND",
    "NX_INVALID_GEOMETRY": "NX_WRONG_GEOMETRY",
    "NX_OBJECT_OWNER_MISMATCH": "NX_WRONG_OWNER",
    "NX_DISPLAY_PART_MISMATCH": "NX_WRONG_PART_CONTEXT",
    "NX_PART_CONTEXT": "NX_WRONG_PART_CONTEXT",
    "NX_EXPLOSION_VIEW_MISMATCH": "NX_WRONG_OWNER",
    "NX_AMBIGUOUS_GEOMETRY": "NX_AMBIGUOUS_REFERENCE",
    "NX_AMBIGUOUS_TARGET": "NX_AMBIGUOUS_REFERENCE",
    "NX_AMBIGUOUS_THREAD": "NX_AMBIGUOUS_REFERENCE",
    "NX_OBJECT_OWNER_AMBIGUOUS": "NX_AMBIGUOUS_REFERENCE",
    "NX_NO_TARGET_BODY": "NX_TARGET_REQUIRED",
    "NX_NO_ACTIVE_SKETCH": "NX_SKETCH_NOT_ACTIVE",
    "NX_NO_PART_OPEN": "NX_NO_WORK_PART",
    "NX_NO_DISPLAY_PART": "NX_NO_WORK_PART",
    "NX_DRAWING_CONTEXT": "NX_WRONG_APPLICATION",
    "NX_DRAWING_ACTIVE": "NX_WRONG_APPLICATION",
    "NX_NO_DRAWING": "NX_WRONG_APPLICATION",
    "NX_NO_COMPONENTS": "NX_NOT_LOADED",
    "NX_UNLOADED_COMPONENT": "NX_NOT_LOADED",
    "NX_ASSEMBLY_NOT_LOADED": "NX_NOT_LOADED",
    "NX_APPLICATION_UNAVAILABLE": "NX_WRONG_APPLICATION",
    "NX_FILE_EXISTS": "NX_ALREADY_EXISTS",
    "NX_NAME_EXISTS": "NX_NAME_CONFLICT",
    "NX_IMPORT_NAME_CONFLICT": "NX_NAME_CONFLICT",
    "NX_MATERIAL_EXISTS": "NX_ALREADY_EXISTS",
    "NX_SECTION_EXISTS": "NX_ALREADY_EXISTS",
    "NX_PARTS_LIST_EXISTS": "NX_ALREADY_EXISTS",
    "NX_REORDER_DEPENDENCY": "NX_DEPENDENCY_ORDER",
    "NX_REORDER_REJECTED": "NX_DEPENDENCY_ORDER",
    "NX_EXPLOSION_IN_USE": "NX_IN_USE",
    "NX_EXPRESSION_READ_ONLY": "NX_READ_ONLY",
    "NX_CROSS_PART_ROLLBACK": "NX_CROSS_PART",
    "NX_RESTORE_ORDER": "NX_DEPENDENCY_ORDER",
    "NX_NOT_CANCELLABLE": "NX_IN_USE",
    "NX_OPERATION_FAILED": "NX_BUILD_FAILED",
    "NX_FACE_EDIT_FAILED": "NX_BUILD_FAILED",
    "NX_INCOMPLETE_SEW": "NX_INCOMPLETE_RESULT",
    "NX_SINGULAR_SURFACE": "NX_BUILD_FAILED",
    "NX_SHEET_METAL_INVALID": "NX_BUILD_FAILED",
    "NX_SKETCH_INVALID": "NX_BUILD_FAILED",
    "NX_DRAWING_INVALID": "NX_BUILD_FAILED",
    "NX_ANNOTATION_INVALID": "NX_BUILD_FAILED",
    "NX_INTERFERENCE_UNRESOLVED": "NX_INCOMPLETE_RESULT",
    "NX_IMPORT_NO_OUTPUT": "NX_INCOMPLETE_RESULT",
    "NX_EXPORT_NO_SOLIDS": "NX_INCOMPLETE_RESULT",
    "NX_MOTION_RESULT_MISSING": "NX_INCOMPLETE_RESULT",
    "NX_MOTION_NOT_ASSOCIATIVE": "NX_INCOMPLETE_RESULT",
    "NX_RELATION_UNSATISFIED": "NX_CONSTRAINT_UNSATISFIED",
    "NX_SKETCH_CONFLICT": "NX_SKETCH_OVERCONSTRAINED",
    "NX_CONSTRAINT_REMOVED": "NX_CONSTRAINT_UNSATISFIED",
    "NX_PATTERN_VERIFICATION_FAILED": "NX_VERIFICATION_FAILED",
    "NX_SAVE_VERIFICATION_FAILED": "NX_VERIFICATION_FAILED",
    "NX_MATERIAL_VERIFICATION_FAILED": "NX_VERIFICATION_FAILED",
    "NX_PLACEMENT_MISMATCH": "NX_VERIFICATION_FAILED",
    "NX_PLACEMENT_CHANGED": "NX_VERIFICATION_FAILED",
    "NX_EXPLOSION_POSITION_MISMATCH": "NX_VERIFICATION_FAILED",
    "NX_FRAME_MISMATCH": "NX_VERIFICATION_FAILED",
    "NX_CAPTURE_RESOLUTION_MISMATCH": "NX_VERIFICATION_FAILED",
    "NX_CLONE_REFERENCE_MISMATCH": "NX_VERIFICATION_FAILED",
    "NX_OBJECT_LIMIT": "NX_TOO_MANY_OBJECTS",
    "NX_SAMPLE_LIMIT": "NX_TOO_MANY_OBJECTS",
    "NX_PAIR_LIMIT": "NX_TOO_MANY_OBJECTS",
    "NX_IMAGE_TOO_LARGE": "NX_RESPONSE_TOO_LARGE",
    "NX_SAVE_PREFLIGHT_FAILED": "NX_SAVE_FAILED",
    "NX_CLONE_FAILED": "NX_EXPORT_FAILED",
    "NX_SCREENSHOT_FAILED": "NX_CAPTURE_FAILED",
    "NX_COMPONENT_LOAD_FAILED": "NX_IMPORT_FAILED",
    "NX_DIRECTORY_ERROR": "NX_TRANSFER_FAILED",
    "NX_CHECKSUM_MISMATCH": "NX_TRANSFER_FAILED",
    "NX_UPLOAD_GAP": "NX_TRANSFER_FAILED",
    "NX_UPLOAD_CONFLICT": "NX_TRANSFER_FAILED",
    "NX_RECEIPT_UNREADABLE": "NX_TRANSFER_FAILED",
    "NX_RESULT_STORAGE_FAILED": "NX_TRANSFER_FAILED",
    "NX_UNAVAILABLE_ON_VERSION": "NX_VERSION_UNSUPPORTED",
    "NX_API_UNAVAILABLE": "NX_VERSION_UNSUPPORTED",
    "NX_UNDO_UNAVAILABLE": "NX_VERSION_UNSUPPORTED",
    "NX_UI_DECLINED": "NX_CONTROL_DECLINED",
    "NX_UI_PAUSED": "NX_CONTROL_PAUSED",
    "NX_PROTOCOL_ERROR": "NX_PROTOCOL_MISMATCH",
    "NX_PROTOCOL_VERSION_MISMATCH": "NX_PROTOCOL_MISMATCH",
    "NX_MAIN_THREAD_UNAVAILABLE": "NX_BRIDGE_UNAVAILABLE",
    "NX_BRIDGE_OUTCOME_UNKNOWN": "NX_OUTCOME_UNKNOWN",
    "NX_SAVE_VIEW_RESTORE_FAILED": "NX_RESTORE_FAILED",
    "NX_RENDER_RESTORE_FAILED": "NX_RESTORE_FAILED",
    "NX_API_ERROR": "NX_ERROR",
    "NX_INTERNAL_ERROR": "NX_ERROR",
}


def dev_mode() -> bool:
    return os.environ.get("NX_MCP_DEV") == "1"


class NXToolError(Exception):
    """A recoverable NX error in the bridge shape (``code``, ``message``, ``details``).

    Raised for bridge-level failures (transport, protocol, unknown method);
    :class:`NXError` subclasses it, so every tool failure crosses the bridge the same way.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        suggestion: str | None = None,
        nx_code: int | str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion
        self.nx_code = nx_code
        self.retryable = retryable
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "error",
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.suggestion:
            result["suggestion"] = self.suggestion
        if self.nx_code is not None:
            result["nx_code"] = self.nx_code
        if self.details:
            result["details"] = self.details
        return result


class NXError(NXToolError):
    """A classified tool failure. Create it with :func:`fail` or :func:`native`."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        hint: str | None = None,
        nx_code: int | None = None,
        details: dict | None = None,
    ) -> None:
        if code not in CODES:
            # A handler raised an unlisted code; the contract test reports it.
            details = {**(details or {}), "unlisted_code": code}
            code = "NX_ERROR"
        category = CODES[code]
        super().__init__(
            code,
            str(message),
            suggestion=hint,
            nx_code=nx_code,
            retryable=CATEGORIES[category][0] in ("after_wait", "after_reacquire"),
            details=dict(details or {}),
        )
        self.category = category
        self.field = field

    @property
    def hint(self) -> str | None:
        return self.suggestion

    @hint.setter
    def hint(self, value) -> None:
        self.suggestion = value

    @property
    def retry(self) -> str:
        return CATEGORIES[self.category][0]

    def as_dict(self) -> dict:
        """The legacy bridge payload, plus ``category`` and ``field``."""
        out = super().as_dict()
        out["category"] = self.category
        if self.field:
            out["field"] = self.field
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
):
    """Raise :class:`NXError` with a stable code from the table (``nx_code`` kept)."""
    raise NXError(code, message, field=field, hint=hint, nx_code=nx_code, details=details)


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
    legacy = getattr(error, "code", None)
    if isinstance(legacy, str) and legacy.startswith("NX_") and hasattr(error, "message"):
        # A legacy NXToolError from a service the kernel still uses.
        new = legacy if legacy in CODES else LEGACY_CODES.get(legacy, "NX_ERROR")
        details = dict(getattr(error, "details", None) or {})
        details.pop("mutation_outcome", None)
        if new != legacy:
            details["legacy_code"] = legacy
        nx_code = getattr(error, "nx_code", None)
        return NXError(
            new,
            str(getattr(error, "message", error)),
            hint=getattr(error, "suggestion", None),
            nx_code=nx_code if isinstance(nx_code, int) else None,
            details=details,
        )
    nx_code = _native_code(error)
    details: dict = {"exception_type": type(error).__name__}
    if dev_mode():
        frames = traceback.format_exception(type(error), error, error.__traceback__)
        details["traceback"] = "".join(frames[-6:])[-4000:]
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
    ``nx_code`` and the native message are always kept; a traceback only with
    ``NX_MCP_DEV=1``.
    """
    try:
        yield
    except NXError:
        raise
    except Exception as error:
        raise classify(error, codes, default) from error

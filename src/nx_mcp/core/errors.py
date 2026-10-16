"""Error taxonomy (tool-surface.md 1.8): 22 categories, 70 codes, ``native()``.

Handlers raise only through :func:`fail` or :func:`native`. The kernel turns any
:class:`NXError` into ``{category, code, message, hint, field, retry, nx_code,
details}``; ``retry`` comes from the category table unless ``fail(retry=...)``
overrides it for one error (a refusal no fix can turn into a success).
"""

from __future__ import annotations

import contextlib
import os
import re
import traceback
import types
from typing import Any, NoReturn

# category -> (retry, codes, default hint)
CATEGORIES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "invalid_argument": (
        "after_fix",
        # NX_IDEMPOTENCY_CONFLICT: an operation_id reused with other arguments (use a new
        # one); nothing ran, so it is not an interruption.
        ("NX_INVALID_ARGUMENT", "NX_INVALID_FORMULA", "NX_IDEMPOTENCY_CONFLICT"),
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
        ("NX_AMBIGUOUS_REFERENCE", "NX_TARGET_REQUIRED", "NX_AMBIGUOUS_PART"),
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
        "Choose another name or path; details.existing, when given, is the object that "
        "has it (use it instead)",
    ),
    "blocked": (
        "after_fix",
        (
            "NX_DEPENDENCY_ORDER",
            "NX_IN_USE",
            "NX_READ_ONLY",
            "NX_CROSS_PART",
            "NX_PART_BUSY",
            "NX_ENGINEER_PART",
        ),
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
        ("NX_BRIDGE_UNAVAILABLE", "NX_BUSY", "NX_PROTOCOL_MISMATCH", "NX_AUTH_FAILED"),
        "NX did not take the call; the message says why (NX_BUSY: NX runs another call; "
        "NX_BRIDGE_UNAVAILABLE: NX or its bridge is not running). nx_get_status shows NX's "
        "state; check nx_get_operation before retrying a mutation",
    ),
    "interrupted": (
        "after_status_check",
        ("NX_CANCELLED", "NX_OUTCOME_UNKNOWN", "NX_CALL_EXPIRED"),
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
# NX_UNVERIFIED      an untested NX build could not answer a check: verify the result
WARNING_CODES = frozenset(
    {
        "NX_UPDATE_WARNING",
        "NX_UNVERIFIED",
        "NX_APPROXIMATED",
        "NX_PARTIAL_PAGE",
        "NX_MODAL_DIALOG",
        "NX_VIEW_OVERLAP",
        "NX_NO_EFFECT",
        "NX_NOTE",
        "NX_TARGET_UNCHANGED",  # a combine target the call left as it was
    }
)
RETRY_VALUES = frozenset(
    {"never", "after_fix", "after_wait", "after_reacquire", "after_status_check", "ask_engineer"}
)

# Native license checkout failures (NX 2406: 948801 missing token feature,
# 949895 license feature not available).
LICENSE_ERROR_CODES = frozenset({948801, 949895})
# NX messages of a license refusal raised under another code (a module or token checkout).
LICENSE_ERROR_TEXT = ("license error", "not enough tokens", "license feature is not available")
# A license feature NX names in a refusal ('feature "nx_sheet_metal"', "feature: drafting",
# "feature nx_freeform_1"): quoted, after a colon, or an unquoted name with an underscore.
# NX 2406's 948801 text names none ("its token feature does not exist").
LICENSE_NAME = re.compile(
    r"""feature\s*(?:[:=]\s*["'(\[]?\s*([A-Za-z][\w.-]*)|["'(\[]\s*([A-Za-z][\w.-]*)\s*["')\]]"""
    r"""|([A-Za-z][A-Za-z0-9]*_[\w-]+))""",
    re.IGNORECASE,
)


def license_named(text: str) -> str | None:
    """The license feature an NX refusal message names, or None."""
    found = LICENSE_NAME.search(str(text or ""))
    return next((g for g in found.groups() if g), None) if found else None


# "Update undo happened.": an update failed and NX undid it, as the host's update
# failure action (host/quiet.py) asks instead of opening "Error During Update".
UPDATE_UNDONE_CODE = 670024
# Native NX codes whose meaning does not depend on the tool (NX 2406 messages): classified
# here unless the tool maps them itself (``native(codes)``), with a hint to fix the input.
# Defaults that say only "NX failed": a known code is more precise. A tool's own default
# (``native(default="NX_INVALID_FORMULA")``) already says what its failures mean.
GENERIC_DEFAULTS = frozenset({"NX_ERROR", "NX_BUILD_FAILED"})
KNOWN_NX_CODES: dict[int, tuple[str, str]] = {
    1050017: (  # "The specified expression variable already exists."
        "NX_ALREADY_EXISTS",
        "Choose a name no expression of the part has (nx_list_objects kind 'expression')",
    ),
    1050011: (  # "New expression variable name is invalid."
        "NX_INVALID_ARGUMENT",
        "An expression name starts with a letter or '_' and holds letters, digits and '_'",
    ),
    1720003: (  # "Sketch already exists with specified name"
        "NX_ALREADY_EXISTS",
        "Choose a name no sketch of the part has (nx_list_objects kind 'sketch')",
    ),
    1405072: (  # "The view is not located on the drawing."
        "NX_INVALID_ARGUMENT",
        "Place the view on the sheet: a position from [0, 0] to the sheet size, sheet units",
    ),
    3270008: (  # "The specified string contains a syntax error"
        "NX_INVALID_FORMULA",
        "Give a number, a unit string such as '10 mm' or a valid NX formula",
    ),
    3520016: (  # "No object found with this name"
        "NX_NOT_FOUND",
        "Use a name NX lists for that kind of object",
    ),
    735029: (  # "There are no drawing sheets in the selected part."
        "NX_NOT_FOUND",
        "Name a part that has drawing sheets",
    ),
}


# ---------------------------------------------------------------------------
# Grammar of messages and panel lines (one place: no "bodys" or "a explosion")

_IRREGULAR = {"body": "bodies", "vertex": "vertices", "datum": "datums", "axis": "axes"}
_AN_EXCEPTIONS = ("one", "use", "uni", "eu")  # 'a unit', 'a one-way'
_AN_LETTERS = frozenset("aeiou")


def article(word: str) -> str:
    """``a`` or ``an`` before ``word`` (``an edge``, ``an explosion``, ``a unit``)."""
    text = str(word).strip().lower()
    if text.startswith(_AN_EXCEPTIONS):
        return "a"
    return "an" if text[:1] in _AN_LETTERS else "a"


def plural(word: str, count: int = 2) -> str:
    """``word`` for one, else its plural; the last word of a phrase (``drawing view`` ->
    ``drawing views``), ``_`` kinds too (``drawing_view`` -> ``drawing_views``)."""
    if count == 1:
        return word
    head, sep, last = str(word).rpartition(" ")
    if not sep:
        head, sep, last = str(word).rpartition("_")
    lower = last.lower()
    if lower in _IRREGULAR:
        out = _IRREGULAR[lower]
    elif lower.endswith(("s", "x", "z", "ch", "sh")):
        out = last + "es"
    elif lower.endswith("y") and lower[-2:-1] not in _AN_LETTERS:
        out = last[:-1] + "ies"
    else:
        out = last + "s"
    return f"{head}{sep}{out}"


def singular(word: str) -> str:
    """The singular of a plural noun (``bodies`` -> ``body``, ``sketches`` -> ``sketch``)."""
    lower = str(word).lower()
    for one, many in _IRREGULAR.items():
        if lower.endswith(many):
            return word[: len(word) - len(many)] + one
    if lower.endswith("ies") and len(lower) > 3:
        return word[:-3] + "y"
    if lower.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if lower.endswith("s") and not lower.endswith("ss"):
        return word[:-1]
    return word


def counted(count: int, word: str) -> str:
    """``1 body``, ``3 bodies``."""
    return f"{count} {plural(word, count)}"


def with_article(word: str) -> str:
    """``an edge``, ``a face``."""
    return f"{article(word)} {word}"


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


def already_exists(
    message: str,
    *,
    field: str | None = None,
    existing: Any = None,
    hint: str | None = None,
    details: dict | None = None,
) -> NoReturn:
    """``NX_ALREADY_EXISTS`` that names the object holding the name or path:
    ``details.existing`` (its result row or path), so the agent can use it instead."""
    out = dict(details or {})
    if existing is not None:
        out["existing"] = existing
    if hint is None:
        hint = (
            "Use details.existing instead, or choose another name"
            if existing is not None
            else "Choose another name or path"
        )
    fail("NX_ALREADY_EXISTS", message, field=field, hint=hint, details=out)


def _native_code(error: BaseException) -> int | None:
    code = getattr(error, "ErrorCode", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def nxopen_member(error: BaseException) -> str | None:
    """``NXOpen.Features.BlockFeatureBuilder.Foo`` when ``error`` is an AttributeError for
    a member an NXOpen object, class or module lacks (``.obj``/``.name``, Python 3.10+);
    None for any other error."""
    if not isinstance(error, AttributeError):
        return None
    name, obj = getattr(error, "name", None), getattr(error, "obj", None)
    if not name or obj is None:
        return None
    if isinstance(obj, types.ModuleType):
        module, owner = obj.__name__, ""
    elif isinstance(obj, type):
        module, owner = str(getattr(obj, "__module__", "")), obj.__qualname__
    else:
        module, owner = str(getattr(type(obj), "__module__", "")), type(obj).__qualname__
    if module != "NXOpen" and not module.startswith("NXOpen."):
        return None
    return ".".join(p for p in (module, owner, str(name)) if p)


def missing_member(error: BaseException | None) -> str | None:
    """The NXOpen member (:func:`nxopen_member`) that ``error`` or an exception it was
    raised from (``__cause__``, ``__context__``) failed to find, else None."""
    seen: set[int] = set()
    pending = [error]
    while pending and len(seen) < 16:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        member = nxopen_member(current)
        if member is None and isinstance(current, NXError):
            member = current.details.get("nx_member")
        if member:
            return str(member)
        pending += [current.__cause__, current.__context__]
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
    text = str(error).casefold()
    if nx_code in LICENSE_ERROR_CODES or (
        nx_code is not None and any(words in text for words in LICENSE_ERROR_TEXT)
    ):
        named = license_named(str(error))
        if named:  # the feature NX named; kit.licenses.attempt adds the one the tool tried
            details["license"] = named
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
        member = nxopen_member(error)
        if member is not None:  # the kernel decides: our bug, or a build without it
            details["nx_member"] = member
        return NXError("NX_ERROR", f"{type(error).__name__}: {error}", details=details)
    if nx_code in KNOWN_NX_CODES and default in GENERIC_DEFAULTS:
        code, hint = KNOWN_NX_CODES[nx_code]
        return NXError(code, str(error), nx_code=nx_code, details=details, hint=hint)
    # NX refused the input for a reason no table names: changing the input is the way on,
    # not a report to the engineer (a Python exception, a bug, stays ``ask_engineer``).
    retry = "after_fix" if default == "NX_ERROR" else None
    return NXError(default, str(error), nx_code=nx_code, details=details, retry=retry)


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

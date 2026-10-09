"""NX-side dispatch: catalogue tools go to the kernel, everything else to legacy.

``HardenedExecutor.execute`` asks :func:`catalogue_spec` first. The catalogue is
loaded at import, so a ``CatalogueError`` stops the host from starting and
``dev_reload.py`` prints it (architecture.md 1.2).

A failed catalogue call is raised as :class:`KernelError`, an ``NXToolError``
whose bridge payload keeps the legacy keys (``code``, ``message``, ``details``
with ``mutation_outcome``) and carries the complete surface 1.6 error envelope
under ``details.envelope`` for the sidecar (step F3).

Part ownership (gate 6) covers both paths during migration: every call owns the
parts it loads (created or opened when not already loaded), and in production
legacy tools that change, save or close a part are refused on parts the agent
does not own (:func:`run_legacy`). The engineer grants a part on the NX MCP panel
(:func:`grant_state`, :func:`set_grant`). ``NX_MCP_DEV=1`` turns the gate off.
"""

from __future__ import annotations

import contextlib
import re

from nx_mcp.core import catalogue, kernel, summary
from nx_mcp.core.errors import NXError
from nx_mcp.core.receipts import new_operation_id, timestamp
from nx_mcp.core.tx import NOT_GRANTED_HINT, check_owned
from nx_mcp.core.validate import validate
from nx_mcp.host.legacy_services import LegacyServices
from nx_mcp.runtime import NXToolError

catalogue.load()

# A catalogue short reference (core.references), e.g. "s:3".
_SHORT_REF = re.compile(r"^[a-z]{1,3}:[1-9][0-9]{0,11}$")
# Arguments that belong to the call, not to the handler.
CALL_KEYS = ("intent", "operation_id", "detail")

# Legacy tools whose new-surface effect is ``view``: they change only what the
# engineer sees, so the ownership gate does not apply (deleted per domain, F4).
LEGACY_VIEW = frozenset(
    {
        "nx_screenshot",
        "nx_render_view",
        "nx_set_camera",
        "nx_set_view",
        "nx_fit_view",
        "nx_set_visibility",
        "nx_set_datum_visibility",
        "nx_set_display",
        "nx_highlight_objects",
        "nx_clear_highlights",
        "nx_restore_display",
        "nx_restore_presentation",
        "nx_section_view",
        "nx_section_control",
    }
)
# Legacy calls that never change an existing part although they are not read-only.
# (Saving and closing parts are catalogue tools; they call tx.require_owned.)
LEGACY_UNGATED = frozenset({"nx_dev_python", "nx_ui_control"})


class KernelError(NXToolError):
    """A failed catalogue call; ``envelope`` is the surface 1.6 error envelope."""

    def __init__(self, envelope: dict) -> None:
        error = envelope["error"]
        super().__init__(
            error["code"],
            error["message"],
            suggestion=error.get("hint"),
            nx_code=error.get("nx_code"),
            retryable=error.get("retry") in ("after_wait", "after_reacquire"),
            details={
                "mutation_outcome": envelope.get("mutation_outcome"),
                "operation_id": envelope.get("operation_id"),
                "category": error.get("category"),
                "envelope": envelope,
            },
        )
        self.envelope = envelope


def catalogue_spec(method: str):
    """The catalogue spec for ``method``, or None for a legacy tool."""
    return catalogue.get(method)


def is_read_only(method: str) -> bool | None:
    """True/False for catalogue tools (by effect), None for legacy tools."""
    spec = catalogue.get(method)
    return None if spec is None else spec.effect == "read"


def effect_of(method: str) -> str | None:
    """The effect of a catalogue tool; ``"view"`` for legacy view tools; else None."""
    spec = catalogue.get(method)
    if spec is not None:
        return spec.effect
    return "view" if method in LEGACY_VIEW else None


def cost_of(method: str) -> str | None:
    """The cost class of a catalogue tool (None for legacy tools)."""
    spec = catalogue.get(method)
    return None if spec is None else spec.cost


def services_for(executor) -> LegacyServices:
    services = getattr(executor, "_catalogue_services", None)
    if services is None:
        services = LegacyServices(executor)
        executor._catalogue_services = services
    return services


def run(executor, spec, params: dict) -> dict:
    """Run one catalogue call; return the envelope or raise :class:`KernelError`."""
    params = dict(params)
    intent = params.pop("intent", None)
    operation_id = params.pop("operation_id", None) or new_operation_id()
    params.pop("detail", None)  # Compaction is the sidecar's job (gate 14).
    services = services_for(executor)
    previous = getattr(executor, "_current_operation", None)
    executor._current_operation = operation_id
    loaded = services.loaded()
    try:
        envelope = kernel.run(
            spec, params, kernel.Context(services, operation_id=operation_id, intent=intent)
        )
    finally:
        executor._current_operation = previous
        with contextlib.suppress(Exception):
            services.track(loaded)
    if envelope.get("ok"):
        return envelope
    raise KernelError(envelope)


# ---------------------------------------------------------------------------
# Legacy tools: the same ownership rule during migration (gate 6)


def _legacy_gated(method: str, params: dict) -> bool:
    from nx_mcp.hardened import NON_MODEL, READ_ONLY

    if method in READ_ONLY or method in NON_MODEL or method in LEGACY_VIEW:
        return False
    return method not in LEGACY_UNGATED


def _legacy_target(executor, method: str, params: dict):
    return executor._work_part(required=False)


def legacy_ids(services, value):
    """Catalogue refs (``"s:3"``) inside legacy arguments become the legacy IDs they wrap.

    Migrated tools return short refs; legacy tools that consume their objects
    (``nx_extrude`` with a sketch from ``nx_create_sketch``) only know ``obj_...``
    IDs. Unknown strings pass through unchanged (deleted with the legacy path, F4).
    """
    if isinstance(value, str):
        entry = services.references.entry(value) if _SHORT_REF.match(value) else None
        return entry.legacy_id if entry is not None else value
    if isinstance(value, list):
        return [legacy_ids(services, v) for v in value]
    if isinstance(value, dict):
        return {k: legacy_ids(services, v) for k, v in value.items()}
    return value


def run_legacy(executor, method: str, params: dict):
    """Run a legacy tool with gate 6 (production) and part tracking (always)."""
    services = services_for(executor)
    params = legacy_ids(services, params)
    gated = not services.dev and _legacy_gated(method, params)
    if gated:
        target = _legacy_target(executor, method, params)
        try:
            check_owned(services, target)
        except NXError as error:
            error.details["mutation_outcome"] = "not_started"
            raise
    flags = services.modified_flags() if gated else None
    loaded = services.loaded()
    try:
        result = executor._execute_legacy(method, params)
    finally:
        with contextlib.suppress(Exception):
            services.track(loaded)
    if flags is not None:
        after = services.modified_flags()
        changed = sorted(k for k, was in flags.items() if not was and after.get(k))
        if changed:
            _undo_foreign_change(executor, result, changed)
    return result


def _undo_foreign_change(executor, result, parts) -> None:
    """A legacy call modified a part the agent does not own: undo it and fail."""
    op_id = result.get("operation_id") if isinstance(result, dict) else None
    history = executor._history
    outcome = "partial"
    if history and history[-1].get("operation_id") == op_id:
        entry = history.pop()
        try:
            executor.session.UndoToMark(entry["mark"], None)
            executor.session.DeleteUndoMark(entry["mark"], None)
            executor.objects.invalidate_part(entry["part_id"])
            outcome = "rolled_back"
        except Exception:
            outcome = "partial"
    error = NXError(
        "NX_PART_NOT_GRANTED",
        "The call modified a part the agent does not own"
        + ("; it was undone" if outcome == "rolled_back" else "; undo failed"),
        hint="Ask the engineer in chat to allow that part on the NX MCP panel",
        details={"parts": parts, "operation_id": op_id, "mutation_outcome": outcome},
    )
    if op_id:
        with contextlib.suppress(Exception):
            record = executor.store.get(op_id)
            record.update(
                state="failed",
                mutation_outcome=outcome,
                error=error.as_dict(),
                finished_at=timestamp(),
            )
            record.pop("result", None)
            executor.store.put(record)
    raise error


# ---------------------------------------------------------------------------
# NX MCP panel: the engineer's grant for the current work part


def grant_state(executor) -> dict | None:
    """The panel's grant checkbox for the work part: None when it is not shown.

    Shown in production only, for a work part the agent neither created nor
    opened: ``{"name": <part name>, "granted": bool}``.
    """
    services = services_for(executor)
    if services.dev:
        return None
    part = services.work_part()
    if part is None or services.created(part):
        return None
    name = str(getattr(part, "Name", "") or services.part_path(part))
    return {"name": name, "granted": services.granted(part)}


def set_grant(executor, allow: bool) -> str | None:
    """Grant (or withdraw) the agent's right to change the work part; its name."""
    services = services_for(executor)
    part = services.work_part()
    if part is None:
        return None
    if allow:
        services.grant(part)
    else:
        services.revoke(part)
    return str(getattr(part, "Name", "") or services.part_path(part))


def render_summary(executor, method: str, params: dict, *, path_of=None) -> str | None:
    """The panel line of a catalogue call (None for legacy tools)."""
    spec = catalogue.get(method)
    if spec is None:
        return None
    services = services_for(executor)
    args = {k: v for k, v in params.items() if k not in CALL_KEYS}
    units = None
    try:
        part = services.work_part()
        units = {"length": services.part_units(part)} if part is not None else None
        if spec.sheet and units is not None:
            checked = validate(spec.params, args)
            _, sheet_units = kernel.sheet_target(spec, checked.args, checked.refs, services)
            units["sheet_length"] = sheet_units or units["length"]
    except Exception:
        pass
    return summary.render(
        spec, args, name_of=services.references.name_of, path_of=path_of, units=units
    )


__all__ = [
    "NOT_GRANTED_HINT",
    "KernelError",
    "catalogue_spec",
    "cost_of",
    "effect_of",
    "grant_state",
    "is_read_only",
    "render_summary",
    "run",
    "run_legacy",
    "services_for",
    "set_grant",
]

"""NX-side dispatch: every public tool is a catalogue tool the kernel runs.

The host (interactive.py, the NX 2406 startup app, the NX 2606 timer journal or
the batch bridge) hands each bridge call to :func:`execute` with its
:class:`~nx_mcp.host.services.NXServices`. The catalogue is loaded at import, so a
``CatalogueError`` stops the host from starting and ``dev_reload.py`` prints it
(architecture.md 1.2).

A failed catalogue call is raised as :class:`KernelError`, an ``NXError``
whose bridge payload keeps the bridge keys (``code``, ``message``, ``details``
with ``mutation_outcome``) and carries the complete surface 1.6 error envelope
under ``details.envelope`` for the sidecar (step F3).

Part ownership (gate 6): every call owns the parts it loads (created or opened
when not already loaded); the engineer grants a part on the NX MCP panel
(:func:`grant_state`, :func:`set_grant`). ``NX_MCP_DEV=1`` turns the gate off.

Development tools (``services.dev_tools``, only ``nx_dev_python`` from the
development launcher) run under one visible undo mark that rolls back on failure.
"""

from __future__ import annotations

import contextlib
import inspect

from nx_mcp.core import catalogue, kernel, summary, target
from nx_mcp.core.errors import NXError, classify
from nx_mcp.core.receipts import new_operation_id
from nx_mcp.core.tx import NOT_GRANTED_HINT
from nx_mcp.core.validate import validate
from nx_mcp.host.quiet import Calm

catalogue.load()

# Arguments that belong to the call, not to the handler. The kernel takes the target
# keys (part, expires_at: core/target.py) itself, unless they are the tool's own.
CALL_KEYS = ("intent", "operation_id", "detail")
TARGET_KEYS = target.CALL_KEYS


class KernelError(NXError):
    """A failed catalogue call; ``envelope`` is the surface 1.6 error envelope."""

    def __init__(self, envelope: dict) -> None:
        error = envelope["error"]
        super().__init__(
            error["code"],
            error["message"],
            hint=error.get("hint"),
            nx_code=error.get("nx_code"),
            retryable=error.get("retry") in ("after_wait", "after_reacquire"),
            retry=error.get("retry"),
            details={
                "mutation_outcome": envelope.get("mutation_outcome"),
                "operation_id": envelope.get("operation_id"),
                "category": error.get("category"),
                "envelope": envelope,
            },
        )
        self.envelope = envelope


def catalogue_spec(method: str):
    """The catalogue spec for ``method``, or None."""
    return catalogue.get(method)


def is_read_only(method: str) -> bool:
    """True for catalogue tools whose effect is ``read``."""
    spec = catalogue.get(method)
    return spec is not None and spec.effect == "read"


def effect_of(method: str) -> str | None:
    """The effect of a catalogue tool, else None."""
    spec = catalogue.get(method)
    return None if spec is None else spec.effect


def cost_of(method: str) -> str | None:
    """The cost class of a catalogue tool, else None."""
    spec = catalogue.get(method)
    return None if spec is None else spec.cost


def execute(services, method: str, params: dict):
    """Run one bridge call: a catalogue tool, or a development tool.

    Under ``services.quiet()`` (host/quiet.py): an NX update failure undoes itself
    instead of opening NX's "Error During Update" dialog, and listing-window output
    goes into the result instead of NX's "Information" window."""
    spec = catalogue.get(method)
    handler = None if spec is not None else services.dev_tools.get(method)
    if spec is None and handler is None:
        raise NXError("NX_UNKNOWN_TOOL", method)
    calm = Calm()
    guard = getattr(services, "quiet", None)
    try:
        with guard(calm) if guard is not None else contextlib.nullcontext():
            if spec is not None:
                result = run(services, spec, params)
            else:
                result = run_dev_tool(services, method, handler, params)
    except NXError as error:
        if calm.listing:
            error.details["listing"] = calm.listing
            envelope = error.details.get("envelope")
            if isinstance(envelope, dict):
                envelope.setdefault("warnings", []).append(_listing_warning(calm.listing))
        raise
    if calm.listing:
        if spec is None:  # a development tool: the snippet's output
            result["stdout"] = str(result.get("stdout") or "") + calm.listing
        else:
            result.setdefault("warnings", []).append(_listing_warning(calm.listing))
    return result


def _listing_warning(text: str) -> dict:
    return {
        "code": "NX_UPDATE_WARNING",
        "message": "NX listing-window output (kept out of NX's Information window): " + text,
    }


def run(services, spec, params: dict) -> dict:
    """Run one catalogue call; return the envelope or raise :class:`KernelError`."""
    params = dict(params)
    intent = params.pop("intent", None)
    operation_id = params.pop("operation_id", None) or new_operation_id()
    params.pop("detail", None)  # Compaction is the sidecar's job (gate 14).
    loaded = services.loaded()
    try:
        envelope = kernel.run(
            spec, params, kernel.Context(services, operation_id=operation_id, intent=intent)
        )
    finally:
        with contextlib.suppress(Exception):
            services.track(loaded)
    if envelope.get("ok"):
        return envelope
    raise KernelError(envelope)


def run_dev_tool(services, method: str, handler, params: dict) -> dict:
    """A development tool under one visible undo mark; a failure is rolled back."""
    operation_id = params.get("operation_id") or new_operation_id()
    params = {k: v for k, v in params.items() if k not in CALL_KEYS + TARGET_KEYS}
    try:
        inspect.signature(handler).bind(**params)
    except TypeError as error:
        raise NXError("NX_INVALID_ARGUMENT", str(error)) from error
    part = services.work_part()
    shown = services.shown_parts()
    before = services.snapshot(part) if part is not None else {}
    mark = services.set_mark("NX MCP: " + method)
    services.epoch += 1
    loaded = services.loaded()
    try:
        result = dict(handler(**params) or {})
    except BaseException as error:  # a snippet's SystemExit is a failed call, not a host exit
        failure = error if isinstance(error, NXError) else classify(error)
        if failure.trace:  # a development tool's caller is the developer: show the stack
            failure.details["traceback"] = failure.trace
        failure.details.update(operation_id=operation_id, **_roll_back_dev(services, mark, shown))
        raise failure from error
    finally:
        with contextlib.suppress(Exception):
            services.track(loaded)  # parts the call created or opened are the agent's
    try:
        after = services.snapshot(part) if part is not None else {}
    except Exception:  # the snippet closed the part it started in
        services.invalidate_parts(services.parts())
        return _dev_result(services, result, operation_id)
    services.invalidate(before, after, True)
    services.record_history(mark, part, operation_id, method)
    return _dev_result(services, result, operation_id)


def _dev_result(services, result: dict, operation_id: str) -> dict:
    return {
        "status": "success",
        **result,
        "operation_id": operation_id,
        "session_id": services.session_id,
        "mutation_outcome": "committed",
        "warnings": list(result.get("warnings", [])),
    }


def _roll_back_dev(services, mark, shown) -> dict:
    """Undo a failed development call to its mark (every part: NX undo is session-wide)
    and show the display and work parts it started with; every step runs even if an
    earlier one fails. ``{"mutation_outcome": ..., "rollback_errors": [...]}``."""
    errors: list[str] = []
    for step, action in (
        ("undo", lambda: services.undo_to(mark)),
        ("delete mark", lambda: services.delete_mark(mark)),
        ("display and work part", lambda: services.show_parts(shown)),
        ("references", lambda: services.invalidate_parts(services.parts())),
        ("update errors", services.clear_update_errors),
    ):
        if step == "delete mark" and errors:
            continue  # the undo failed: keep the mark for a manual undo
        try:
            action()
        except Exception as error:
            errors.append(f"{step}: {error}")
    out: dict = {"mutation_outcome": "partial" if errors else "rolled_back"}
    if errors:
        out["rollback_errors"] = errors
    return out


# ---------------------------------------------------------------------------
# NX MCP panel: the engineer's grant for the current work part


def grant_state(services) -> dict | None:
    """The panel's grant checkbox for the work part: None when it is not shown.

    Shown in production only, for a work part the agent neither created nor
    opened: ``{"name": <part name>, "granted": bool}``.
    """
    if services.dev:
        return None
    part = services.work_part()
    if part is None or services.created(part):
        return None
    name = str(getattr(part, "Name", "") or services.part_path(part))
    return {"name": name, "granted": services.granted(part)}


def set_grant(services, allow: bool) -> str | None:
    """Grant (or withdraw) the agent's right to change the work part; its name."""
    part = services.work_part()
    if part is None:
        return None
    if allow:
        services.grant(part)
    else:
        services.revoke(part)
    return str(getattr(part, "Name", "") or services.part_path(part))


def render_summary(services, method: str, params: dict, *, path_of=None) -> str | None:
    """The panel line of a catalogue call (None for other methods)."""
    spec = catalogue.get(method)
    if spec is None:
        return None
    args, _keys = target.split_call_keys(spec, params)
    args = {k: v for k, v in args.items() if k not in CALL_KEYS}
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
    "execute",
    "grant_state",
    "is_read_only",
    "render_summary",
    "run",
    "run_dev_tool",
    "set_grant",
]

"""NX-side dispatch: catalogue tools go to the kernel, everything else to legacy.

``HardenedExecutor.execute`` asks :func:`catalogue_spec` first. The catalogue is
loaded at import, so a ``CatalogueError`` stops the host from starting and
``dev_reload.py`` prints it (architecture.md 1.2).

A failed catalogue call is raised as :class:`KernelError`, an ``NXToolError``
whose bridge payload keeps the legacy keys (``code``, ``message``, ``details``
with ``mutation_outcome``) and carries the complete surface 1.6 error envelope
under ``details.envelope`` for the sidecar (step F3).
"""

from __future__ import annotations

from nx_mcp.core import catalogue, kernel, summary
from nx_mcp.core.receipts import new_operation_id
from nx_mcp.host.legacy_services import LegacyServices
from nx_mcp.runtime import NXToolError

catalogue.load()

# Arguments that belong to the call, not to the handler.
CALL_KEYS = ("intent", "operation_id", "detail")


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
    try:
        envelope = kernel.run(
            spec, params, kernel.Context(services, operation_id=operation_id, intent=intent)
        )
    finally:
        executor._current_operation = previous
    if envelope.get("ok"):
        return envelope
    raise KernelError(envelope)


def after_legacy(executor, method: str, result) -> None:
    """Record parts the agent created or newly opened through legacy tools (gate 6)."""
    if method not in ("nx_create_part", "nx_open_part") or not isinstance(result, dict):
        return
    if method == "nx_open_part" and result.get("already_loaded", True):
        return
    part = executor._work_part(required=False)
    if part is not None:
        services_for(executor).own(part)


def render_summary(executor, method: str, params: dict, *, path_of=None) -> str | None:
    """The panel line of a catalogue call (None for legacy tools)."""
    spec = catalogue.get(method)
    if spec is None:
        return None
    services = services_for(executor)
    units = None
    try:
        part = services.work_part()
        units = {"length": services.part_units(part)} if part is not None else None
    except Exception:
        units = None
    args = {k: v for k, v in params.items() if k not in CALL_KEYS}
    return summary.render(
        spec, args, name_of=services.references.name_of, path_of=path_of, units=units
    )

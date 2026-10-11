"""Gate 4s: make the call's target part current in NX (architecture.md 3.1).

Gate 3t (``core/target.py``) names the part a call acts on; this gate makes NX act there
and puts the engineer's state back when the call ends (:func:`enter`, :meth:`Bound.leave`):

- ``none`` (``spec.switch_for``: reads): no NX change. A target that is not the work
  part is *bound* (``Services.bound``): ``tx.part``, ``Services.work_part``, units,
  ``requires`` and sheets follow it; ``tx.scratch`` makes it the work part quietly for
  its block.
- ``work`` (model tools): the target becomes the work part (``Services.switch``:
  ``Parts.SetWork`` for the display part or a component prototype under it, else
  ``uf.Assem.SetWorkPartQuietly``); the display does not move.
- ``display`` (drawings, captures, cameras, the Sheet Metal application, variants that
  need the displayed assembly): the target is displayed (undo marks kept) and the
  application the tool needs is entered; the previous display, work part and
  application come back.

No switch, no binding and no NX call when the target *is* the work part (one part
loaded, or the call targets the work part), or when it is the display part and the call
did not name it (``part``): a call in the displayed assembly's context behaves as before.
``NX_MCP_SWITCH=display`` makes model tools switch the display instead of the work part.

After the handler the work part must still be the target (:meth:`Bound.check`, before
commit). A restore that fails falls back to the home state and becomes a warning on the
envelope (the change may be committed); a state NX did not take back is retried at the
next call's entry (:func:`repair`). Display-bound refreshers (gate 13: drawing views,
tables, dimensions, annotations) do not run on a target that is not displayed: they are
deferred to the next call that refreshes that part while it is displayed
(:meth:`Bound.defers`, :func:`deferred`).
"""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass, field
from typing import Any

from nx_mcp.core import target as targets
from nx_mcp.core.errors import NXError

ENV = "NX_MCP_SWITCH"  # "display": model tools switch the display part too
DISPLAY_REFRESHERS = frozenset({"drawing_view", "drawing_table", "dimension", "annotation"})
LOG_KEPT = 500  # switch log entries kept (Services.switch_log)
DEFERRED_NOTE = (
    "{file} is not displayed, so its drawing views and annotations were not refreshed "
    "(NX updates them only on the displayed part); they refresh on its next drawing call or "
    "nx_update_annotations, and out-of-date views when it is saved"
)
LEAK_NOTE = "NX MCP first restored the display and work parts an earlier call left switched"


def configured_mode() -> str:
    """``display`` when ``NX_MCP_SWITCH=display`` (the visible fallback), else ``work``."""
    return "display" if os.environ.get(ENV, "").strip().lower() == "display" else "work"


def same(services, a, b) -> bool:
    if a is None or b is None:
        return False
    try:
        return int(a.Tag) == int(b.Tag)
    except Exception:
        try:
            return services.part_key(a) == services.part_key(b)
        except Exception:
            return a is b


def shown(services) -> tuple:
    """``(display part, work part)`` as NX has them (never the bound part)."""
    parts = getattr(getattr(services, "session", None), "Parts", None)
    if parts is None or not hasattr(parts, "Work"):
        work = services.work_part()
        return work, work
    return getattr(parts, "Display", None), getattr(parts, "Work", None)


def displayed(services, part) -> bool:
    """Whether NX displays ``part``."""
    return same(services, shown(services)[0], part)


def file_name(part) -> str:
    full = str(getattr(part, "FullPath", "") or "")
    return full.replace("\\", "/").rsplit("/", 1)[-1] if full else str(getattr(part, "Name", ""))


def _application(spec) -> str | None:
    return next((r.split(":", 1)[1] for r in spec.requires if r.startswith("application:")), None)


@dataclass
class Bound:
    """A call's gate 4s state: what was switched or bound, and the notes it leaves."""

    services: Any
    target: Any = None  # core.target.Target
    tool: str | None = None
    mode: str = "none"
    switch: Any = None  # the services' switch (kit.switching.Switch), entered
    bound: bool = False  # Services.bound was set for the call
    previous: Any = None  # Services.bound before the call (a nested call's parent)
    notes: list = field(default_factory=list)  # (code, message) for the call's warnings
    started: float = field(default_factory=time.perf_counter)
    left: bool = False

    @property
    def switched(self) -> bool:
        return self.switch is not None

    @property
    def background(self) -> bool:
        """The call switched the work part to a target that is not displayed."""
        if self.switch is None or self.target is None:
            return False
        display, _work = shown(self.services)
        return not same(self.services, display, self.target.part)

    def check(self) -> None:
        """After the handler: the switched target is still the work part (else the call
        fails before commit and rolls back)."""
        if self.switch is None or self.target is None:
            return
        services, part = self.services, self.target.part
        _display, work = shown(services)
        if same(services, work, part):
            return
        try:
            loaded = any(same(services, p, part) for p in services.parts())
        except Exception:
            loaded = True
        if not loaded:
            return  # the call unloaded its part (an undo across its load)
        raise NXError(
            "NX_WRONG_PART_CONTEXT",
            f"The call left {file_name(work)} as the work part instead of {file_name(part)}; "
            "it was undone",
            details={"part": file_name(part), "work_part": file_name(work)},
        )

    def defers(self, kind: str) -> bool:
        """Whether gate 13 skips the refresher of ``kind`` on this call (display-bound
        refresher, target not displayed); records the part for the next refresh."""
        if kind not in DISPLAY_REFRESHERS or not self.background:
            return False
        deferred(self.services).add(self.target.key)
        return True

    def leave(self) -> str | None:
        """Undo the binding and restore the switch; the warning text, or None."""
        if self.left:
            return None
        self.left = True
        services = self.services
        if self.bound:
            services.bound = self.previous
        if self.switch is None:
            return None
        failure = None
        try:
            failure = self.switch.restore()
        except Exception as error:  # a services switch that raises: still a warning
            failure = str(error) or type(error).__name__
        clean = bool(getattr(self.switch, "clean", True))
        if not clean:
            services.__dict__["_switch_leak"] = self.switch
        _log(services, self, failure)
        if failure is None:
            return None
        return (
            f"NX MCP could not fully restore the display and work parts after working in "
            f"{self.target.file}: {failure}"
        )


def deferred(services) -> set:
    """Part keys whose display-bound refreshers a background call skipped."""
    return services.__dict__.setdefault("_deferred_refresh", set())


def has_drafting(part) -> bool:
    """Whether the part has drawing sheets, dimensions or notes (what gate 13 refreshes)."""
    for name in ("DrawingSheets", "Dimensions", "Notes"):
        with contextlib.suppress(Exception):
            if any(True for _ in getattr(part, name, None) or []):
                return True
    return False


def repair(services) -> str | None:
    """At a call's entry: retry the restore an earlier call could not finish."""
    leak = services.__dict__.pop("_switch_leak", None)
    if leak is None:
        return None
    try:
        clean = bool(leak.retry())
    except Exception:
        clean = False
    if not clean:
        return f"{LEAK_NOTE}, but NX did not take them back; check with nx_list_parts"
    return LEAK_NOTE


def enter(spec, args, target, services, *, explicit: bool, nested: bool = False) -> Bound:
    """Gate 4s. ``explicit``: the call named its part (``part``). ``nested``: a batch or
    preview child of a call that switched NX to its own part: the child switches whenever
    its target is not the work part, or not displayed for a display-bound child. Raises
    ``NX_WRONG_PART_CONTEXT`` when NX refuses the switch (nothing ran)."""
    bound = Bound(services, target, spec.name)
    note = None if nested else repair(services)
    if note:
        bound.notes.append(("NX_NOTE", note))
    if target is None or target.source == "arg":
        return bound  # no target, or the tool's own part argument: the handler's part
    display, work = shown(services)
    mode = spec.switch_for(args, getattr(target, "kinds", ()))
    if mode == "work" and configured_mode() == "display":
        mode = "display"
    if nested:
        if same(services, target.part, work) and (
            mode != "display" or same(services, target.part, display)
        ):
            return bound  # the parent's part, or shown already: no NX call
    elif same(services, target.part, work):
        return bound  # the work part: as before, no NX call
    elif same(services, target.part, display) and (mode == "display" or not explicit):
        return bound  # displayed already (a work component under it stays): as before
    if not _loaded(services, target.part):
        return bound  # an object of a part NX does not list: the handler reports it
    bound.mode = mode
    if mode == "none":
        bound.previous, bound.bound = services.bound, True
        services.bound = target.part
        return bound
    switch = services.switch(target.part, mode, application=_application(spec))
    if switch is None:  # services that cannot switch (P0 rule): refuse a named part
        bound.mode = "none"
        if explicit:
            targets.refuse_other_part(spec, target, services, explicit=True)
        return bound
    bound.switch = switch
    return bound


def _loaded(services, part) -> bool:
    try:
        return any(same(services, p, part) for p in services.parts() or [])
    except Exception:
        return False


def _log(services, bound: Bound, failure) -> None:
    switch = bound.switch
    timings = getattr(switch, "timings", {}) or {}
    log = services.switch_log()
    log.append(
        {
            "tool": bound.tool,
            "part": bound.target.file if bound.target is not None else None,
            "mode": bound.mode,
            "primitive": getattr(switch, "primitive", None),
            "switch_ms": round(1000 * float(timings.get("switch", 0.0)), 2),
            "restore_ms": round(1000 * float(timings.get("restore", 0.0)), 2),
            "call_ms": round(1000 * (time.perf_counter() - bound.started), 2),
            "restored": failure is None,
        }
    )
    del log[:-LOG_KEPT]


__all__ = [
    "Bound",
    "configured_mode",
    "deferred",
    "displayed",
    "enter",
    "has_drafting",
    "repair",
    "shown",
]

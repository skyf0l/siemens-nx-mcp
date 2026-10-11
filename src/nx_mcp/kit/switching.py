"""Make a call's target part current in NX, then put the engineer's state back.

    sw = switching.Switch(tx, part, "work")       # or "display"
    sw.enter()                                    # NX_WRONG_PART_CONTEXT when NX refuses
    ...                                           # builders now build in ``part``
    warning = sw.restore()                        # None, or what could not be restored

Gate 4s of the kernel (``core/switching.py``) drives it through ``Services.switch``;
``tx`` is anything with ``session``, ``nx``, ``uf`` and ``workspace``.

NX 2406 findings encoded here (probe_display, judge S1/S2):

- Builders build in the work part (drafting in the display part), whatever collection
  they come from: the target must be the work part (model tools) or the display part
  (drawings, captures, cameras, the sheet-metal application).
- ``Parts.SetWork`` accepts only the display part or a component prototype under it
  ("Part or occurrence is not a descendant of the root part"); then it is the official
  switch (``work`` mode, ``SetWork``: about 40 ms, the Part Navigator shows the part).
- ``uf.Assem.SetWorkPartQuietly`` makes any loaded part the work part without touching
  the UI (about 0 ms; graphics and navigator keep the display part): ``quiet``. It must
  be undone before control goes back to the engineer, so ``restore`` always runs.
- ``kit.parts.activate`` is the display switch (``display`` mode: keeps NX's undo
  marks, no Sheet Metal dialog, 140-190 ms round trip, two repaints). Camera, current
  sheet and undo marks survive the round trip.
- The NX application is one per session and sticky across display switches: a call
  that needs Sheet Metal enters it on the target (``application``), and ``restore``
  puts the previous application back unless that would show NX's blocking Sheet Metal
  warning (a part with sheet-metal features entering Modeling): then it stays and says so.
- Restore failure: the home state (the previous display part, then its work part) is
  forced with ``SetWorkPartQuietly(display)`` + ``SetWork(display)``; ``restore``
  returns the text of a warning instead of raising (the call may have committed).
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from nx_mcp.core.errors import NXError
from nx_mcp.kit import parts as kparts

APPLICATIONS = {"sheet_metal": kparts.SHEET_METAL_APP, "modeling": "UG_APP_MODELING"}
MODELING_APP = "UG_APP_MODELING"


def same(a, b) -> bool:
    """Same NX part (by tag)."""
    try:
        return a is not None and b is not None and int(a.Tag) == int(b.Tag)
    except Exception:
        return a is b


def file_name(part) -> str:
    full = str(getattr(part, "FullPath", "") or "")
    return full.replace("\\", "/").rsplit("/", 1)[-1] if full else str(getattr(part, "Name", ""))


def quiet_work(tx, part) -> None:
    """Make ``part`` the work part without the UI (``uf.Assem.SetWorkPartQuietly``)."""
    assem = getattr(tx.uf, "Assem", None)
    quietly = getattr(assem, "SetWorkPartQuietly", None)
    if quietly is None:
        raise NXError("NX_WRONG_PART_CONTEXT", "This NX cannot make a part work quietly")
    quietly(part.Tag)


def set_work_component(tx, component) -> None:
    """``Parts.SetWorkComponent`` keeping the reference sets (NX 2406 signature)."""
    parts = tx.session.Parts
    collection = getattr(tx.nx, "PartCollection", None)
    if collection is None:
        status = parts.SetWorkComponent(component)
    else:
        status = parts.SetWorkComponent(
            component, collection.RefsetOption.Current, collection.WorkComponentOption.Given
        )
    if status is not None and hasattr(status, "Dispose"):
        with contextlib.suppress(Exception):
            status.Dispose()


class Switch:
    """One call's switch: ``enter`` makes ``part`` current, ``restore`` puts back the
    display part, the work part (and work component) and the application it found.

    ``mode`` ``work``: the target becomes the work part (``SetWork`` when NX allows it,
    else ``quiet``); with nothing displayed it is displayed (nothing to restore then).
    ``mode`` ``display``: the target is displayed and made the work part; ``application``
    (``sheet_metal``) is entered when the call needs it. ``primitive`` names what was
    done (``noop``, ``SetWork``, ``quiet``, ``display``) and ``timings`` the seconds of
    ``switch`` and ``restore``."""

    def __init__(self, tx, part, mode: str, *, application: str | None = None) -> None:
        self.tx = tx
        self.part = part
        self.mode = mode
        self.application = application
        self.primitive = "noop"
        self.timings = {"switch": 0.0, "restore": 0.0}
        self.display: Any = None  # what enter() found: the display part, work part,
        self.work: Any = None  # work component and application
        self.component: Any = None
        self.app: str | None = None
        self.entered = False  # NX state changed: restore must run
        self.restored = False

    # -- enter ---------------------------------------------------------------------------
    def enter(self) -> Switch:
        tx, part = self.tx, self.part
        parts = tx.session.Parts
        self.display, self.work = parts.Display, parts.Work
        self.component = getattr(parts, "WorkComponent", None)
        self.app = str(getattr(tx.session, "ApplicationName", "") or "")
        started = time.perf_counter()
        try:
            self.entered = True
            if self.mode == "display" or self.display is None:
                self._display()
            elif same(self.display, part) or kparts.can_be_work(tx, part):
                parts.SetWork(part)  # the official switch: the display part or a prototype
                self.primitive = "SetWork"
            else:
                quiet_work(tx, part)
                self.primitive = "quiet"
            if not same(parts.Work, part):
                raise NXError(
                    "NX_WRONG_PART_CONTEXT",
                    f"NX did not make {file_name(part)} the work part",
                    details={"work_part": file_name(parts.Work)},
                )
        except Exception as error:
            failure = self.restore()
            if isinstance(error, NXError):
                if failure:
                    error.details.setdefault("restore", failure)
                raise
            raise NXError(
                "NX_WRONG_PART_CONTEXT",
                f"NX could not make {file_name(part)} current: {error}",
                hint="Activate the part with nx_activate_part, then repeat the call",
                details={"restore": failure} if failure else {},
            ) from error
        finally:
            self.timings["switch"] = time.perf_counter() - started
        return self

    def _display(self) -> None:
        tx, part = self.tx, self.part
        kparts.activate(tx, part)  # keeps undo marks; never NX's Sheet Metal dialog
        self.primitive = "display"
        wanted = APPLICATIONS.get(self.application or "")
        session = tx.session
        if wanted and str(session.ApplicationName or "") != wanted:
            if wanted == MODELING_APP and kparts.has_sheet_metal(part):
                return  # NX's blocking warning: gate 4 refuses the call instead
            session.ApplicationSwitchImmediate(wanted)
            kparts.keep_sheet_metal(tx)

    # -- restore -------------------------------------------------------------------------
    def restore(self) -> str | None:
        """Put back what ``enter`` found; None, or the text of what stayed different
        after the home-state fallback (never raises)."""
        if not self.entered or self.restored:
            return None
        self.restored = True
        started = time.perf_counter()
        problems: list[str] = []
        try:
            try:
                self._restore()
            except Exception as error:
                problems.append(str(error) or type(error).__name__)
                self._home(problems)
            notes = self._application(problems)
            return "; ".join(problems + notes) or None
        finally:
            self.timings["restore"] = time.perf_counter() - started

    @property
    def clean(self) -> bool:
        """Whether NX shows the display and work parts ``enter`` found."""
        if not self.entered or self.display is None:
            return True
        parts = self.tx.session.Parts
        return same(parts.Display, self.display) and same(parts.Work, self.work or self.display)

    def retry(self) -> bool:
        """Try the restore again (a later call's entry repair); True when clean."""
        with contextlib.suppress(Exception):
            self._restore()
        if not self.clean:
            self._home([])
        return self.clean

    def _restore(self) -> None:
        tx = self.tx
        parts = tx.session.Parts
        if self.display is None:
            return  # nothing was displayed: the target stays shown (as nx_activate_part)
        if not same(parts.Display, self.display):
            kparts.activate(tx, self.display, work=False)
        self._work()
        if not same(parts.Display, self.display) or not same(parts.Work, self.work):
            raise NXError(
                "NX_RESTORE_FAILED",
                f"NX shows {file_name(parts.Display)} with work part {file_name(parts.Work)}",
            )

    def _work(self) -> None:
        parts = self.tx.session.Parts
        if self.work is None or same(parts.Work, self.work):
            return
        component = self.component
        if component is not None and same(getattr(component, "Prototype", None), self.work):
            with contextlib.suppress(Exception):
                set_work_component(self.tx, component)
                if same(parts.Work, self.work):
                    return
        parts.SetWork(self.work)

    def _home(self, problems: list) -> None:
        """The fallback: the previous display part as display and work part, then its
        previous work part when NX takes it."""
        tx = self.tx
        parts = tx.session.Parts
        home = self.display
        if home is None:
            return
        for step in (
            lambda: None if same(parts.Display, home) else kparts.show(tx.nx, parts, home),
            lambda: quiet_work(tx, home),
            lambda: parts.SetWork(home),
            self._work,
        ):
            try:
                step()
            except Exception as error:
                problems.append(f"home state: {error}")
        if same(parts.Display, home) and same(parts.Work, self.work or home):
            problems.append(
                f"the home state was restored ({file_name(home)} displayed and work part)"
                if same(home, self.work or home)
                else f"the home state was restored ({file_name(home)} displayed, work part "
                f"{file_name(self.work)})"
            )

    def _application(self, problems: list) -> list[str]:
        """Put the previous application back after a display switch (one per session)."""
        tx = self.tx
        session = tx.session
        if self.primitive != "display" or not self.app or self.display is None:
            return []
        now = str(getattr(session, "ApplicationName", "") or "")
        if now == self.app:
            return []
        shown = session.Parts.Display
        if self.app == MODELING_APP and kparts.has_sheet_metal(shown):
            return [
                f"NX stays in {now} for {file_name(shown)}: it has sheet-metal features and "
                "NX would block on its Sheet Metal warning in Modeling"
            ]
        try:
            session.ApplicationSwitchImmediate(self.app)
            kparts.keep_sheet_metal(tx)
        except Exception as error:
            problems.append(f"application {self.app}: {error}")
        return []


__all__ = ["Switch", "quiet_work", "same", "set_work_component"]

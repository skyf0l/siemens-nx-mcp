"""Loaded parts: lookup by file, basename clashes, activation, components, saving.

    part = parts.loaded(tx, path)                  # the loaded part of this file, or None
    parts.check_new_name(tx, path)                 # NX_ALREADY_EXISTS / NX_NAME_CONFLICT
    parts.activate(tx, part, work=True, display=True)  # its own NX tab, undo marks kept
    active = parts.present(tx, part, display=None)  # tab policy of create/open/activate
    parts.explicit(tx, part)                       # loaded by a call of its own
    for component, names in parts.components(part): ...
    with parts.saving(tx, part) as updated:        # drawing preview data survives the save
        part.Save(...)                             # updated: views it brought up to date

NX 2406 findings encoded here:

- NX names a part by its file basename: a loaded part with the same basename
  (any folder, saved or not) makes ``FileNew``/``SaveAs``/open fail with "File
  already exists". ``check_new_name`` refuses before NX acts.
- A part whose drawing sheets keep CGM preview data saves them only while a
  sheet is displayed: ``saving`` opens the current (or first) sheet, saves,
  then restores the sheet, the modeling display and the work/display parts.
- Views saved out of date reopen with an "(Out of Date)" sheet banner: ``saving``
  updates the views NX flags out of date before the save.
- Displaying a part never drops NX's undo marks here: a plain ``SetDisplay`` (or a
  load that displays: ``FileNew`` with ``MakeDisplayedPart``, ``OpenBaseDisplay``)
  resets the whole undo list whenever a live mark is older than the first display of
  the shown or hidden part (measured on NX 2406), and a plain ``Close`` of any part
  drops every mark too. ``show`` is the one display switch:
  ``SetActiveDisplayRetainUndoMarks(AllowAdditional, SameAsDisplay)`` keeps every mark
  and also makes the part the work part. Loads go through ``OpenBase`` or
  ``FileNew(MakeDisplayedPart=False)``, which leave the display and work parts as they
  were.
- Tabs (NX 2406, tabs probe): ``AllowAdditional`` shows each part in its own NX window
  tab and never replaces the part of another tab; for a part that has a tab it only
  activates that tab (63-92 ms; a new tab 0.2-0.9 s). ``Parts.GetDisplayedParts`` lists
  the tabs, the active one (``Parts.Display``) first, then the most recently active. A
  drawing sheet stays open in its tab. ``CloseRetainUndoMarks`` of a part closes its tab
  with the marks kept; closing the active tab leaves NX with no display part
  (``UG_APP_NOPART``) until a tab is activated again. NX's application stays one per
  session across tab switches.
- ``present`` is the tab policy of nx_create_part, nx_open_part and nx_activate_part:
  while the part is the agent's only one (rule G's count) its tab stays active as
  before; with two or more agent parts loaded the part gets its tab but the tab active
  before comes back (nx_create_part does not display it at all), unless the call asks
  (``display`` true).
- NX remembers an application per part: the one last entered with
  ``ApplicationSwitchImmediate`` while it was displayed (a new part keeps its
  template's Modeling). From no displayed part (``UG_APP_NOPART``, after the
  display part was closed), a display switch enters the part's own application, and
  closing any part re-enters the display part's own application. When that is
  Modeling for a part with sheet-metal features, NX opens the modal "Sheet Metal"
  warning ("Features added in the Modeling environment may not be properly
  depicted in a flat pattern...") and blocks NX and every call until someone
  answers it. Changing the display part while an application runs keeps that
  application, without a dialog. So: re-entering Sheet Metal on the displayed part
  records it (``keep_sheet_metal``, before a close too), and a sheet-metal part is
  displayed from no display part through a scratch part already in Sheet Metal.
"""

from __future__ import annotations

import contextlib
import uuid
from pathlib import Path

from nx_mcp.core.errors import fail
from nx_mcp.core.references import deleted
from nx_mcp.kit import drawing


def file_of(part) -> str:
    """The part's full path ('' for None or a part NX has no file name for).

    A part NX no longer has (closed, or undone by NX itself) fails as
    ``NX_STALE_REFERENCE``: NX would raise "Object no longer exists"."""
    if part is not None and deleted(part):
        fail(
            "NX_STALE_REFERENCE",
            "The part is no longer loaded in NX",
            hint="List the loaded parts again with nx_list_parts",
            details={"reason": "deleted_in_nx"},
        )
    return str(getattr(part, "FullPath", "") or "")


def _key(path) -> str:
    try:
        return str(Path(path).resolve()).casefold()
    except (OSError, ValueError):
        return str(path).casefold()


def same_file(a, b) -> bool:
    return bool(a) and bool(b) and _key(a) == _key(b)


def same_part(a, b) -> bool:
    """Same NX part (by tag: NXOpen may hand out different wrappers for one part)."""
    return a is not None and b is not None and int(a.Tag) == int(b.Tag)


def loaded(tx, path):
    """The loaded part whose file is ``path``, or None."""
    return next((p for p in tx.session.Parts if same_file(file_of(p), path)), None)


def _taken(tx, ignore=None) -> set[str]:
    """Casefolded file stems of the loaded parts (other than ``ignore``)."""
    return {Path(file_of(p)).stem.casefold() for p in tx.session.Parts if not same_part(p, ignore)}


def unique_name(tx, path, *, ignore=None) -> Path:
    """``path`` with ``_2``, ``_3``, ... added to its stem: the first name no loaded part
    uses and no file in its folder has."""
    path = Path(path)
    taken = _taken(tx, ignore)
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix or '.prt'}")
        if candidate.stem.casefold() not in taken and not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}_{uuid.uuid4().hex[:8]}{path.suffix or '.prt'}")


def check_new_name(tx, path, *, field: str | None = "/path", ignore=None) -> None:
    """Refuse a new part file whose basename a loaded part (other than ``ignore``) uses.

    NX names a loaded part by its file name, so the name is taken whoever loaded the
    part (another agent's part too): the error suggests a unique name
    (``details.suggested_path``) and never offers the loaded part."""
    stem = Path(path).stem.casefold()
    clash = next(
        (
            p
            for p in tx.session.Parts
            if not same_part(p, ignore) and Path(file_of(p)).stem.casefold() == stem
        ),
        None,
    )
    if clash is None:
        return
    same = same_file(file_of(clash), path)
    name = Path(file_of(clash)).name
    suggested = unique_name(tx, path, ignore=ignore)
    try:
        shown = str(Path(suggested).resolve().relative_to(Path(tx.workspace.root).resolve()))
    except Exception:
        shown = str(suggested)
    shown = shown.replace("\\", "/")
    fail(
        "NX_ALREADY_EXISTS" if same else "NX_NAME_CONFLICT",
        (
            f"The file name {name} is taken: that file is already loaded as a part"
            if same
            else f"The file name {name} is taken: a loaded part in another folder uses it "
            "(NX names parts by file name)"
        ),
        field=field,
        hint=f"Choose a unique file name, e.g. {shown}",
        details={"path": str(path), "loaded_part_path": file_of(clash), "suggested_path": shown},
    )


def in_workspace(tx, part, *, field: str | None = "/part", allow_unsaved: bool = False):
    """The part's file inside the workspace (``NX_PATH_OUTSIDE_WORKSPACE`` otherwise).

    ``allow_unsaved`` admits a part NX has no absolute file name for yet (None).
    """
    path = file_of(part)
    if not path or not Path(path).is_absolute():
        if allow_unsaved:
            return None
        fail(
            "NX_PATH_OUTSIDE_WORKSPACE",
            "The part has no file in the workspace yet; save it under a new path first",
            field=field,
            details={"path": path or None},
        )
    try:
        return tx.workspace.ensure_inside(path)
    except Exception as error:
        fail(
            "NX_PATH_OUTSIDE_WORKSPACE",
            f"{error}; this part's file is not in the workspace: {path}",
            field=field,
            details={"path": path},
        )


def _dispose(status) -> None:
    if status is not None and hasattr(status, "Dispose"):
        with contextlib.suppress(Exception):
            status.Dispose()


def can_be_work(tx, part) -> bool:
    """Whether ``part`` may become the work part while the display part stays: NX
    accepts only the display part itself or a component prototype under it."""
    display = tx.session.Parts.Display
    if display is None or same_part(display, part):
        return True
    return any(same_part(getattr(c, "Prototype", None), part) for c, _ in components(display))


def check_work_only(tx, part, *, field: str | None = "/display") -> None:
    """Refuse work-without-display for a part outside the display part's assembly
    (NX 2406: "Part or occurrence is not a descendant of the root part"). ``part``
    None is a part not loaded yet, hence no component of the display part."""
    if tx.session.Parts.Display is None or (part is not None and can_be_work(tx, part)):
        return
    fail(
        "NX_WRONG_PART_CONTEXT",
        "Only the display part or one of its components can be the work part",
        field=field,
        hint="Display the part too (display=true)",
    )


SHEET_METAL_APP = "UG_APP_SBSM"
SHEET_METAL_FEATURES = ("SB_", "FLAT_PATTERN", "FLAT_SOLID")
SCRATCH_TEMPLATE = "model-plain-1-mm-template.prt"
# Blank modeling templates of a new part by units.
TEMPLATES = {"mm": SCRATCH_TEMPLATE, "in": "model-plain-1-inch-template.prt"}


def has_sheet_metal(part) -> bool:
    """True when ``part`` holds a sheet-metal body or feature (Sheet Metal ``SB_`` types)."""
    if part is None:
        return False
    if any(str(getattr(f, "FeatureType", "")).startswith(SHEET_METAL_FEATURES)
           for f in part.Features):  # fmt: skip
        return True
    manager = getattr(part.Features, "SheetmetalManager", None)
    return manager is not None and any(manager.IsSheetmetalBody(b) for b in part.Bodies)


def keep_sheet_metal(tx) -> None:
    """Record Sheet Metal as the displayed sheet-metal part's own application.

    Only while NX runs Sheet Metal; re-entering it shows no dialog (about 0.2 s).
    Without it, NX re-enters the part's remembered Modeling on the next close or
    display and opens its blocking "Sheet Metal" warning.
    """
    session = tx.session
    display = session.Parts.Display
    if str(session.ApplicationName or "") == SHEET_METAL_APP and has_sheet_metal(display):
        session.ApplicationSwitchImmediate(SHEET_METAL_APP)


@contextlib.contextmanager
def keeping_application(tx):
    """Run a block that may leave Sheet Metal, then re-enter it if NX left it.

    Back in Sheet Metal the displayed part also records it (``keep_sheet_metal``), so
    no later close or display opens NX's warning. Only a changed application is
    re-entered: NX 2406 closes a shown drawing sheet on ``ApplicationSwitchImmediate``
    (live: leaving and reopening a sheet keeps Sheet Metal), so run this inside
    ``drawing.in_modeling``, which reopens the sheet afterwards.
    """
    session = tx.session
    before = str(session.ApplicationName or "")
    failed = True
    try:
        yield
        failed = False
    finally:
        now = str(session.ApplicationName or "")
        if before == SHEET_METAL_APP != now and has_sheet_metal(session.Parts.Display):
            with contextlib.suppress(Exception) if failed else contextlib.nullcontext():
                session.ApplicationSwitchImmediate(SHEET_METAL_APP)


def show(nx, parts, part) -> None:
    """Show ``part`` in its own NX window tab, or activate the tab it has, and make it
    the work part, keeping NX's undo marks: the only display switch of NX MCP (a grep
    test forbids any other). The part of another tab is never replaced."""
    result = parts.SetActiveDisplayRetainUndoMarks(
        part,
        nx.DisplayPartOption.AllowAdditional,
        nx.PartDisplayPartWorkPartOption.SameAsDisplay,
    )
    for status in result if isinstance(result, tuple) else (result,):
        _dispose(status)


def tabs(session_parts) -> list:
    """The parts NX shows in window tabs, the active one first, then the most recently
    active (``Parts.GetDisplayedParts``; the display part alone where NX has no tabs)."""
    shown: list = []
    getter = getattr(session_parts, "GetDisplayedParts", None)
    if getter is not None:
        with contextlib.suppress(Exception):
            shown = [p for p in getter() or [] if p is not None]
    display = getattr(session_parts, "Display", None)
    if display is not None and not any(same_part(p, display) for p in shown):
        shown.insert(0, display)
    return shown


def _display_in_sheet_metal(tx, part) -> None:
    """Display a sheet-metal part while NX has no display part, without NX's dialog.

    NX would enter the part's remembered application, possibly Modeling (the
    blocking warning). A scratch part (never saved) is displayed and switched to
    Sheet Metal first; the part then keeps Sheet Metal and is recorded in it before
    the scratch part closes. Every step keeps NX's undo marks: the scratch part is
    created without display, shown with ``show`` and closed with
    ``CloseRetainUndoMarks`` once it is no longer displayed.
    """
    session = tx.session
    parts = session.Parts
    name = f"nx_mcp_scratch_{uuid.uuid4().hex[:12]}.prt"
    builder = parts.FileNew()
    try:
        builder.TemplateFileName = SCRATCH_TEMPLATE
        builder.Units = tx.nx.Part.Units.Millimeters
        builder.NewFileName = str(Path(tx.workspace.root) / ".nx-mcp" / name)
        builder.MakeDisplayedPart = False
        builder.DisplayPartOption = tx.nx.DisplayPartOption.AllowAdditional
        scratch = builder.Commit()
    finally:
        with contextlib.suppress(Exception):
            builder.Destroy()
    try:
        show(tx.nx, parts, scratch)
        session.ApplicationSwitchImmediate(SHEET_METAL_APP)
        show(tx.nx, parts, part)
        session.ApplicationSwitchImmediate(SHEET_METAL_APP)  # before the scratch part closes
    finally:
        scratch.CloseRetainUndoMarks(
            tx.nx.BasePart.CloseWholeTree.FalseValue,
            tx.nx.BasePart.CloseModified.CloseModified,
            None,
        )


def activate(tx, part, *, work: bool = True, display: bool = True) -> None:
    """Make ``part`` the display and/or work part (display first: NX moves work too).

    Keeps NX's undo marks (``show``) and never lets NX open its Sheet Metal
    application dialog (module notes).
    """
    parts = tx.session.Parts
    if display and parts.Display != part:
        if parts.Display is None and has_sheet_metal(part):
            _display_in_sheet_metal(tx, part)
        else:
            show(tx.nx, parts, part)
    if display:
        keep_sheet_metal(tx)
    if work and parts.Work != part:
        parts.SetWork(part)


def crowded(tx, part=None) -> bool:
    """Whether two or more agent parts are loaded, ``part`` included (rule G's count,
    ``core/target.agent_parts``: created, opened or granted; never the engineer's)."""
    from nx_mcp.core import target as targets

    services = getattr(tx, "services", None)
    if services is None:
        return False
    try:
        agents = targets.agent_parts(services)
    except Exception:
        return False
    extra = part is not None and not any(same_part(p, part) for p in agents)
    return len(agents) + int(extra) >= 2


def present(tx, part, *, work: bool = True, display: bool | None = None) -> bool:
    """Show ``part`` as nx_open_part and nx_activate_part ask; True when its tab is left
    active (then it is also the work part when ``work``).

    ``display`` False: no tab (the work part only, as ``activate``). True: its tab, left
    active. None (the default): its tab, left active while it is the agent's only part;
    else the tab active before comes back with its work part, so a call never takes the
    engineer's (or another agent's) tab once two or more agent parts are loaded."""
    if display is False:
        activate(tx, part, work=work, display=False)
        return False
    session_parts = tx.session.Parts
    before, before_work = session_parts.Display, session_parts.Work
    keep = display is True or not crowded(tx, part)
    activate(tx, part, work=work, display=True)
    if keep or before is None or same_part(before, part):
        return True
    activate(tx, before, work=False)  # the tab active before comes back
    if before_work is not None and not same_part(session_parts.Work, before_work):
        with contextlib.suppress(Exception):
            if can_be_work(tx, before_work):
                session_parts.SetWork(before_work)
    return False


EXPLICIT = "_explicit_parts"  # Services attribute: keys of parts loaded by a call of their own


def explicit(tx, part) -> None:
    """Record that a call created or opened ``part`` by name (nx_create_part,
    nx_open_part, an import's new part): closing an assembly never unloads it."""
    services = getattr(tx, "services", None)
    if services is None or part is None:
        return
    with contextlib.suppress(Exception):
        services.__dict__.setdefault(EXPLICIT, set()).add(services.part_key(part))


def is_explicit(tx, part) -> bool:
    """Whether a call of its own created or opened ``part`` (:func:`explicit`); a part an
    assembly's load or a component call brought in is not."""
    services = getattr(tx, "services", None)
    if services is None:
        return False
    try:
        return services.part_key(part) in services.__dict__.get(EXPLICIT, ())
    except Exception:
        return False


def components(part) -> list:
    """``[(component, [names from the root])]`` of every occurrence, depth first."""
    assembly = getattr(part, "ComponentAssembly", None)
    root = getattr(assembly, "RootComponent", None)
    out: list = []

    def walk(parent, names):
        for child in parent.GetChildren():
            path = [*names, str(getattr(child, "Name", "") or child.JournalIdentifier)]
            out.append((child, path))
            walk(child, path)

    if root is not None:
        walk(root, [])
    return out


def prototype_file(component) -> str:
    """The prototype's file of an occurrence ('' when the prototype is not loaded)."""
    return file_of(getattr(component, "Prototype", None))


def dependents(tx, target) -> list[str]:
    """Files of loaded assemblies that use ``target`` as a component prototype."""
    tag = int(target.Tag)
    out = []
    for part in tx.session.Parts:
        if part == target:
            continue
        for component, _ in components(part):
            prototype = getattr(component, "Prototype", None)
            if prototype is not None and int(prototype.Tag) == tag:
                out.append(file_of(part))
                break
    return out


def load_components(tx, part, *, limit: int = 10000) -> int:
    """Fully load every unsuppressed occurrence prototype; restores partial loading.

    Returns the number of occurrences checked. Unloadable prototypes raise
    ``NX_IMPORT_FAILED`` with the native status per file.
    """
    options = tx.session.Parts.LoadOptions
    partial = options.UsePartialLoading
    opened: set[int] = set()
    try:
        options.UsePartialLoading = False
        while True:
            pending = [
                c for c, _ in components(part) if not c.IsSuppressed and int(c.Tag) not in opened
            ]
            if not pending:
                break
            if len(opened) + len(pending) > limit:
                fail("NX_TOO_MANY_OBJECTS", f"Loading exceeds {limit} occurrences")
            status, _ = part.ComponentAssembly.OpenComponents(
                part.ComponentAssembly.OpenOption.ComponentOnly, pending
            )
            try:
                if status and status.NumberUnloadedParts:
                    fail(
                        "NX_IMPORT_FAILED",
                        "Some component prototypes could not be loaded",
                        details={
                            "parts": [
                                {"path": status.GetPartName(i), "nx_code": status.GetStatus(i)}
                                for i in range(status.NumberUnloadedParts)
                            ]
                        },
                    )
            finally:
                _dispose(status)
            opened.update(int(c.Tag) for c in pending)
            if hasattr(tx, "checkpoint"):
                tx.checkpoint()
    finally:
        options.UsePartialLoading = partial
    missing = [
        {"occurrence": "/".join(names), "path": prototype_file(c) or None}
        for c, names in components(part)
        if not c.IsSuppressed and not getattr(c.Prototype, "IsFullyLoaded", bool(prototype_file(c)))
    ]
    if missing:
        fail("NX_NOT_LOADED", "Some occurrences are still not loaded", details={"missing": missing})
    return len(opened)


def _sheet_views(sheets) -> list:
    views: list = []
    for sheet in sheets:
        with contextlib.suppress(Exception):  # a sheet NX cannot list views of
            views.extend(sheet.GetDraftingViews())
    return views


@contextlib.contextmanager
def saving(tx, part, *, force_display: bool = False, update_views: bool = True):
    """Keep a part's drawing preview data through a save; restore the display after.

    Yields the drawing views it updated: with ``update_views`` (default) the views NX
    flags out of date on any sheet are updated first, so the saved part does not reopen
    with "(Out of Date)" sheets. Without drawing sheets, or when the part keeps no CGM
    preview data, has no out-of-date view and ``force_display`` is False, nothing
    changes. A failed restore raises ``NX_RESTORE_FAILED`` (the save itself already
    happened).
    """
    sheets = list(getattr(part, "DrawingSheets", None) or [])
    keeps_cgm = bool(getattr(getattr(part, "SaveOptions", None), "DrawingCgmData", False))
    views = _sheet_views(sheets) if sheets and update_views else []
    stale = drawing.out_of_date(views)
    if not sheets or (not keeps_cgm and not force_display and not stale):
        yield []
        return
    session = tx.session
    if session.ActiveSketch is not None:
        fail(
            "NX_SKETCH_ACTIVE",
            "Finish the active sketch before saving a part with drawing sheets",
            hint="Finish it with nx_finish_sketch",
        )
    work, display = session.Parts.Work, session.Parts.Display
    original = drawing.current(part)
    moved = work != part or display != part

    def restore():
        if original is None:
            drawing.close_sheets(tx, part)
        elif drawing.current(part) != original:
            drawing.open_sheet(tx, original, part)
        if moved:
            if display is not None:
                activate(tx, display, work=False)
            if work is not None:
                activate(tx, work, display=False)

    try:
        if moved:
            activate(tx, part)
        drawing.open_sheet(tx, original or sheets[0], part)
        yield drawing.update_out_of_date(tx, stale) if stale else []
    except BaseException:
        with contextlib.suppress(Exception):
            restore()  # never hides the error already propagating
        raise
    try:
        restore()
    except Exception as error:
        fail(
            "NX_RESTORE_FAILED",
            f"The part was saved but the drawing display was not restored: {error}",
        )

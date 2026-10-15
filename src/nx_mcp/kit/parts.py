"""Loaded parts: lookup by file, basename clashes, activation, components, saving.

    part = parts.loaded(tx, path)                  # the loaded part of this file, or None
    parts.check_new_name(tx, path)                 # NX_ALREADY_EXISTS / NX_NAME_CONFLICT
    parts.activate(tx, part, work=True, display=True)  # its own NX tab, undo marks kept
    shown = parts.present(tx, part, display=None)  # tab policy of create/open/activate
    engineer = parts.engineer_tab(tx)              # the engineer's displayed part, or None
    parts.explicit(tx, part)                       # loaded by a call of its own
    parts.require_loaded(part, message)            # NX_NOT_LOADED: a component not fully loaded
    count, unloaded = parts.load_all(tx, part)     # load every component, as saved
    with parts.loading_as_saved(tx): ...           # components load from their saved folders
    for component, names in parts.components(part): ...
    with parts.saving(tx, part) as updated:        # drawing preview data survives the save
        part.Save(...)                             # updated: views it brought up to date
    with parts.without_cgm([template]):            # NX writes files we cannot display
        manager.SaveFamilyAndCreateMembers(...)

NX 2406 findings encoded here:

- NX names a part by its file basename: a loaded part with the same basename
  (any folder, saved or not) makes ``FileNew``/``SaveAs``/open fail with "File
  already exists". ``check_new_name`` refuses before NX acts.
- A part whose drawing sheets keep CGM preview data saves them only while a
  sheet is displayed: ``saving`` opens the current (or first) sheet, saves,
  then restores the sheet, the modeling display and the work/display parts.
- Saving a part that keeps CGM preview data while none of its sheets is displayed
  shows NX's modal "Save CGM" dialog ("Parts containing sheets that are not visible
  will not have a CGM saved..."), which blocks the host. Saves NX runs itself, where
  no sheet can be displayed first (family members written from their template), go
  through ``without_cgm``: the files they write carry no CGM preview.
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
- Component load method (``LoadOptions.ComponentLoadMethod``, NX 2406 enum AsSaved 0,
  FromDirectory 1, SearchDirectories 2): sessions often run ``FromDirectory``, which looks
  for a component only in the assembly's own folder, so one saved in a subfolder fails
  with status 641044 ("not found"). Opens and loads switch to ``AsSaved`` for the call
  (``loading_as_saved``) and try the assembly's folder tree as search folders after that.
- ``present`` is the tab policy of nx_create_part, nx_open_part and nx_activate_part.
  The engineer tab guard first: while the displayed part is the engineer's
  (``engineer_tab``: not created, opened or granted to the agent), an agent part is
  never left active and the work part never changes, whatever ``display`` says
  (``display`` true: its own tab behind the engineer's, ``tab_behind``). Otherwise,
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
    """``path`` with ``-2``, ``-3``, ... added to its stem (the suffix the path gate
    suggests too): the first name no loaded part uses and no file in its folder has."""
    path = Path(path)
    taken = _taken(tx, ignore)
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix or '.prt'}")
        if candidate.stem.casefold() not in taken and not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}-{uuid.uuid4().hex[:8]}{path.suffix or '.prt'}")


def shown_path(tx, path) -> str:
    """``path`` workspace-relative with forward slashes (as given when outside it)."""
    try:
        return Path(path).resolve().relative_to(Path(tx.workspace.root).resolve()).as_posix()
    except Exception:
        return Path(str(path)).as_posix()


def _part_row(tx, part):
    """The loaded part's ref row (None when the call cannot make one)."""
    try:
        return tx.ref(part, "part")
    except Exception:
        return None


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
    shown = shown_path(tx, unique_name(tx, path, ignore=ignore))
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
        details={
            "path": shown_path(tx, path),
            "loaded_part": _part_row(tx, clash),
            "loaded_part_path": shown_path(tx, file_of(clash)),
            "suggested_path": shown,
        },
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
    accepts only the display part itself or a component prototype under it. With no
    display part NX refuses ``SetWork`` (NX 2406: 720037 "There Is No Displayed Part")."""
    display = tx.session.Parts.Display
    if display is None:
        return False
    if same_part(display, part):
        return True
    return any(same_part(getattr(c, "Prototype", None), part) for c, _ in components(display))


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


def engineer_tab(tx):
    """The displayed part when it is the engineer's (loaded by the engineer: not created,
    opened or granted to the agent, ``Services.owns``), else None: no display part, an
    agent part, or services that cannot tell. While it is set, no call leaves an agent
    part active (the engineer tab guard, :func:`present`)."""
    display = tx.session.Parts.Display
    services = getattr(tx, "services", None)
    if display is None or services is None:
        return None
    try:
        return None if services.owns(display) else display
    except Exception:
        return None


def has_tab(tx, part) -> bool:
    """Whether ``part`` has its own NX window tab (active or not)."""
    return any(same_part(p, part) for p in tabs(tx.session.Parts))


def tab_behind(tx, part) -> None:
    """Give ``part`` its own NX tab (or keep the one it has) while the tab active before
    stays active with its work part: show it, then re-activate that tab (63-92 ms)."""
    session_parts = tx.session.Parts
    before, before_work = session_parts.Display, session_parts.Work
    activate(tx, part, work=False)
    if before is None or same_part(before, part):
        return
    give_back(tx, before, before_work)


def give_back(tx, display, work=None) -> None:
    """Re-activate ``display``'s tab and put ``work`` back as its work part when NX takes
    it (the display part or a component under it)."""
    session_parts = tx.session.Parts
    if not same_part(session_parts.Display, display):
        activate(tx, display, work=False)
    if work is not None and not same_part(session_parts.Work, work):
        with contextlib.suppress(Exception):
            if can_be_work(tx, work):
                session_parts.SetWork(work)


class Presented:
    """What :func:`present` did: ``active`` (its tab is the active one, so it is the
    display part), ``work`` (it is the work part), ``kept`` (the display part that stayed
    active instead, None when ``part`` is active or nothing is displayed) and
    ``engineer`` (``kept`` is the engineer's part: the guard applied)."""

    def __init__(self, active: bool, work: bool, kept=None, engineer: bool = False) -> None:
        self.active, self.work, self.kept, self.engineer = active, work, kept, engineer


def present(tx, part, *, work: bool = True, display: bool | None = None) -> Presented:
    """Show ``part`` as nx_open_part and nx_activate_part ask.

    The engineer tab guard comes first: while the display part is the engineer's
    (:func:`engineer_tab`), an agent part is never left active and the work part never
    changes, whatever ``display`` and ``work`` say: ``display`` true gives the part its
    own tab behind the engineer's (:func:`tab_behind`), else it stays in the background.
    Calls reach it by their ``part`` key; the kernel switches to it for each call.

    Otherwise ``display`` False: no tab; the work part when ``work`` and NX takes it
    (the display part or a component under it; else the work part stays). True: its tab,
    left active. None (the default): its tab, left active while it is the agent's only
    part; else the tab active before comes back with its work part, so a call never takes
    another agent's tab once two or more agent parts are loaded."""
    engineer = engineer_tab(tx)
    if engineer is not None and not same_part(engineer, part):
        if display is True:
            tab_behind(tx, part)
        return Presented(False, False, engineer, True)
    session_parts = tx.session.Parts
    before, before_work = session_parts.Display, session_parts.Work
    if display is False:
        made = work and can_be_work(tx, part)
        if made:
            activate(tx, part, work=True, display=False)
        kept = None if before is None or same_part(before, part) else before
        return Presented(same_part(before, part), made, kept)
    keep = display is True or not crowded(tx, part)
    activate(tx, part, work=work, display=True)
    if keep or before is None or same_part(before, part):
        return Presented(True, work)
    give_back(tx, before, before_work)  # the tab active before comes back
    return Presented(False, same_part(session_parts.Work, part), before)


def shown_note(tx, part, presented: Presented) -> str:
    """The note of a create, open or activate that left ``part`` out of the active tab:
    where it is and how calls reach it."""
    from nx_mcp.core.target import file_name

    name = file_name(part)
    kept = file_name(presented.kept) if presented.kept is not None else None
    where = "in its own NX tab behind it" if has_tab(tx, part) else "in the background"
    head = (
        f"The engineer's {kept} stays the active NX tab"
        if presented.engineer
        else f"{kept} stays the active NX tab"
        if kept
        else "No NX tab is active"
    )
    return (
        f"{head}; {name} is loaded {where}. Calls reach it with "
        f'"part": "{name}" (no display or activation needed)'
    )


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


def suppressed(component) -> bool:
    """True when the component or one of its parents is suppressed."""
    node = component
    while node is not None:
        if getattr(node, "IsSuppressed", False):
            return True
        node = getattr(node, "Parent", None)
    return False


LOAD_HINT = "Open the assembly with nx_open_part(load_components=true) and retry"


def unloaded(part) -> list[str]:
    """Paths (names from the root, joined by ``/``) of the unsuppressed components of
    ``part`` whose part is not loaded or only partially loaded."""
    return [
        "/".join(names)
        for component, names in components(part)
        if not suppressed(component)
        and not (prototype_file(component) and getattr(component.Prototype, "IsFullyLoaded", True))
    ]


def require_loaded(part, message: str, hint: str = LOAD_HINT) -> None:
    """``NX_NOT_LOADED`` (``message``, the first 50 component paths) when a component of
    ``part`` is not fully loaded: exports write only what is loaded."""
    missing = unloaded(part)
    if missing:
        fail("NX_NOT_LOADED", message, hint=hint, details={"components": missing[:50]})


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


# Native load status codes of a component part NX did not load (PartLoadStatus).
LOAD_STATUS = {
    641044: "file not found where NX looked for it (its saved folder or the assembly's)",
}


def load_reason(code) -> str:
    """Plain words for a native component load status."""
    try:
        return LOAD_STATUS.get(int(code), f"NX could not load it (NX status {int(code)})")
    except (TypeError, ValueError):
        return "NX could not load it"


def load_failures(status) -> list[dict]:
    """``[{file, nx_code, reason}]`` of the parts a ``PartLoadStatus`` names as not loaded
    (``[]`` for None or a status without failures)."""
    out: list[dict] = []
    for i in range(int(getattr(status, "NumberUnloadedParts", 0) or 0) if status else 0):
        with contextlib.suppress(Exception):
            code = status.GetStatus(i)
            out.append({
                "file": Path(str(status.GetPartName(i))).name,
                "nx_code": int(code),
                "reason": load_reason(code),
            })  # fmt: skip
    return out


@contextlib.contextmanager
def loading_as_saved(tx, folder=None):
    """Load component parts from the folder each was saved in (``LoadMethod.AsSaved``),
    whatever the session's load option says, then put the option back.

    NX 2406 sessions often load components ``FromDirectory``: only the assembly's own
    folder, so a component saved in a subfolder fails with status 641044. With ``folder``
    the components load from the search folders instead: ``folder`` and its subfolders
    (``LoadMethod.SearchDirectories``; for a tree moved after it was saved), with the
    session's search folders put back too.
    """
    options = getattr(tx.session.Parts, "LoadOptions", None)
    methods = getattr(getattr(tx.nx, "LoadOptions", None), "LoadMethod", None)
    before = getattr(options, "ComponentLoadMethod", None)
    searchable = hasattr(options, "GetSearchDirectories") and hasattr(
        options, "SetSearchDirectories"
    )
    if options is None or methods is None or before is None or (folder and not searchable):
        yield
        return
    searched = None
    try:
        if folder is not None:
            searched = options.GetSearchDirectories()
            options.SetSearchDirectories([str(folder)], [True])
            options.ComponentLoadMethod = methods.SearchDirectories
        else:
            options.ComponentLoadMethod = methods.AsSaved
        yield
    finally:
        with contextlib.suppress(Exception):
            options.ComponentLoadMethod = before
        if searched is not None:
            with contextlib.suppress(Exception):
                dirs, subs = searched
                options.SetSearchDirectories(list(dirs), list(subs))


def _not_loaded(part) -> list:
    """Unsuppressed occurrences of ``part`` whose prototype is not fully loaded."""
    return [
        (c, names)
        for c, names in components(part)
        if not getattr(c, "IsSuppressed", False)
        and not getattr(getattr(c, "Prototype", None), "IsFullyLoaded", bool(prototype_file(c)))
    ]


def outside_folder_note(assembly, component_file) -> str | None:
    """The note for a component whose file is not in the assembly's folder (a subfolder
    or another folder), else None. NX MCP loads components as saved
    (:func:`loading_as_saved`); an NX session whose load option is "From Folder" looks only
    in the assembly's folder and reports the component as not found."""
    top, file = file_of(assembly), str(component_file or "")
    if not top or not file:
        return None
    try:
        if Path(file).resolve().parent == Path(top).resolve().parent:
            return None
    except (OSError, ValueError):
        return None
    return (
        f"{Path(file).name} is not in the assembly's folder: NX MCP loads it from the folder "
        "it was saved in, but an NX session that loads components from the assembly's folder "
        "will not find it. Keep component files beside the assembly to share it that way"
    )


def load_all(tx, part, *, limit: int = 10000) -> tuple[int, list[dict]]:
    """Fully load every unsuppressed occurrence prototype, as saved; restores partial
    loading. ``(occurrences checked, unloaded rows)``: a row per occurrence still not
    loaded, ``{component, file, nx_code, reason}``. Occurrences NX could not find as saved
    are tried once more from the assembly's folder and its subfolders. Never raises for a
    part NX could not load."""
    options = tx.session.Parts.LoadOptions
    partial = options.UsePartialLoading
    opened: set[int] = set()
    failures: dict[str, dict] = {}

    def open_all(pending) -> None:
        status, _ = part.ComponentAssembly.OpenComponents(
            part.ComponentAssembly.OpenOption.ComponentOnly, pending
        )
        try:
            for row in load_failures(status):
                failures[row["file"].casefold()] = row
        finally:
            _dispose(status)

    try:
        options.UsePartialLoading = False
        with loading_as_saved(tx):
            while True:
                pending = [
                    c
                    for c, _ in components(part)
                    if not c.IsSuppressed and int(c.Tag) not in opened
                ]
                if not pending:
                    break
                if len(opened) + len(pending) > limit:
                    fail("NX_TOO_MANY_OBJECTS", f"Loading exceeds {limit} occurrences")
                open_all(pending)
                opened.update(int(c.Tag) for c in pending)
                if hasattr(tx, "checkpoint"):
                    tx.checkpoint()
        missing = _not_loaded(part)
        folder = Path(file_of(part)).parent if file_of(part) else None
        if missing and folder is not None:
            with loading_as_saved(tx, folder):
                open_all([c for c, _ in missing])
            missing = _not_loaded(part)
    finally:
        options.UsePartialLoading = partial
    rows = []
    for component, names in missing:
        file = Path(prototype_file(component)).name if prototype_file(component) else None
        if file is None:  # an unloaded component still knows its file (UF, as saved)
            with contextlib.suppress(Exception):
                file = Path(str(tx.uf.Assem.AskComponentData(component.Tag)[0])).name or None
        failure = failures.get((file or "").casefold())
        if failure is None and len(failures) == 1 and len(missing) == 1:
            failure = next(iter(failures.values()))
        rows.append({
            "component": "/".join(names),
            "file": file or (failure or {}).get("file"),
            "nx_code": (failure or {}).get("nx_code"),
            "reason": (failure or {}).get("reason") or "not loaded",
        })  # fmt: skip
    return len(opened), rows


def load_components(tx, part, *, limit: int = 10000) -> int:
    """:func:`load_all`, failing with ``NX_IMPORT_FAILED`` (``details.parts``: each
    unloaded component with NX's status in words) when any occurrence stays unloaded."""
    count, missing = load_all(tx, part, limit=limit)
    if missing:
        fail(
            "NX_IMPORT_FAILED",
            "Some component prototypes could not be loaded",
            hint="Check that the component files exist (nx_list_files); NX looked in the "
            "folder each was saved in and under the assembly's folder",
            details={"parts": missing[:50]},
        )
    return count


def _sheet_views(sheets) -> list:
    views: list = []
    for sheet in sheets:
        with contextlib.suppress(Exception):  # a sheet NX cannot list views of
            views.extend(sheet.GetDraftingViews())
    return views


def unchanged_on_disk(part) -> bool:
    """Whether a save of ``part`` would write nothing new: not modified, its file exists
    and no drawing view is out of date (a save brings those up to date first)."""
    if bool(getattr(part, "IsModified", True)):
        return False
    path = file_of(part)
    if not path or not Path(path).is_file():
        return False
    sheets = list(getattr(part, "DrawingSheets", None) or [])
    return not (sheets and drawing.out_of_date(_sheet_views(sheets)))


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
            _restore_parts(tx, display, work)

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


def set_work(tx, work) -> None:
    """Make ``work`` the work part again: ``SetWork`` where NX takes it (the display part or
    a component under it), else quietly (``uf.Assem.SetWorkPartQuietly``: a work part
    outside the display part, as gate 4s leaves it). NX refuses ``SetWork`` there with
    "Part or occurrence is not a descendant of the root part"."""
    session_parts = tx.session.Parts
    if same_part(session_parts.Work, work):
        return
    if can_be_work(tx, work):
        session_parts.SetWork(work)
        return
    quietly = getattr(getattr(tx.uf, "Assem", None), "SetWorkPartQuietly", None)
    if quietly is None:
        session_parts.SetWork(work)  # NX's own error says why
        return
    quietly(work.Tag)


def _restore_parts(tx, display, work) -> None:
    """Put the display and work parts a save found back. The save already happened, so a
    part NX will not take back is a note, never a failure: the call's own switch (gate 4s)
    gives the engineer's display and work part back after the call."""
    try:
        if display is not None and not same_part(tx.session.Parts.Display, display):
            activate(tx, display, work=False)
        if work is not None:
            set_work(tx, work)
    except Exception as error:
        warn = getattr(tx, "warn", None)
        if warn is None:
            raise
        warn(
            "NX_NOTE",
            f"The part was saved; NX did not take back the display or work part it had "
            f"before ({error}). Calls reach parts by their part key, so nothing else changes",
        )


@contextlib.contextmanager
def without_cgm(targets):
    """Turn off ``SaveOptions.DrawingCgmData`` of ``targets`` for a save NX runs itself.

    NX shows the modal "Save CGM" dialog when it saves a part that keeps CGM preview
    data while its sheets are not displayed; ``saving`` avoids it by displaying a sheet,
    which saves NX writes on its own (family members, clones) cannot use. Parts without
    the option or with it off are left alone; the old values are restored after, also
    on failure. A failed restore raises ``NX_RESTORE_FAILED``.
    """
    changed = []
    try:
        for part in targets:
            options = getattr(part, "SaveOptions", None)
            if options is not None and bool(getattr(options, "DrawingCgmData", False)):
                options.DrawingCgmData = False
                changed.append(options)
        yield list(changed)
    except BaseException:
        for options in changed:
            with contextlib.suppress(Exception):  # never hides the error propagating
                options.DrawingCgmData = True
        raise
    try:
        for options in changed:
            options.DrawingCgmData = True
    except Exception as error:
        fail(
            "NX_RESTORE_FAILED",
            f"The files were written but the CGM save option was not restored: {error}",
        )

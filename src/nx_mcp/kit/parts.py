"""Loaded parts: lookup by file, basename clashes, activation, components, saving.

    part = parts.loaded(tx, path)                  # the loaded part of this file, or None
    parts.check_new_name(tx, path)                 # NX_ALREADY_EXISTS / NX_NAME_CONFLICT
    parts.activate(tx, part, work=True, display=True)
    for component, names in parts.components(part): ...
    with parts.saving(tx, part):                   # drawing preview data survives the save
        part.Save(...)

NX 2406 findings encoded here:

- NX names a part by its file basename: a loaded part with the same basename
  (any folder, saved or not) makes ``FileNew``/``SaveAs``/open fail with "File
  already exists". ``check_new_name`` refuses before NX acts.
- A part whose drawing sheets keep CGM preview data saves them only while a
  sheet is displayed: ``saving`` opens the current (or first) sheet, saves,
  then restores the sheet, the modeling display and the work/display parts.
- ``SetDisplay`` also changes the work part; set the work part afterwards.
- NX remembers an application per part: the one last entered with
  ``ApplicationSwitchImmediate`` while it was displayed (a new part keeps its
  template's Modeling). From no displayed part (``UG_APP_NOPART``, after the
  display part was closed), ``SetDisplay`` enters the part's own application, and
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
from nx_mcp.kit import drawing


def file_of(part) -> str:
    """The part's full path ('' for a part NX has no file name for)."""
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


def check_new_name(tx, path, *, field: str | None = "/path", ignore=None) -> None:
    """Refuse a new part file whose basename a loaded part (other than ``ignore``) uses."""
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
    fail(
        "NX_ALREADY_EXISTS" if same else "NX_NAME_CONFLICT",
        (
            "This part is already loaded; activate it with nx_activate_part"
            if same
            else "A loaded part already uses this file name; choose another name"
        ),
        field=field,
        details={"path": str(path), "loaded_part_path": file_of(clash)},
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


def _set_display(parts, part) -> None:
    _, status = parts.SetDisplay(part, False, False)
    _dispose(status)


def _display_in_sheet_metal(tx, part) -> None:
    """Display a sheet-metal part while NX has no display part, without NX's dialog.

    NX would enter the part's remembered application, possibly Modeling (the
    blocking warning). A scratch part (never saved) is displayed and switched to
    Sheet Metal first; the part then keeps Sheet Metal and is recorded in it before
    the scratch part closes.
    """
    session = tx.session
    parts = session.Parts
    name = f"nx_mcp_scratch_{uuid.uuid4().hex[:12]}.prt"
    builder = parts.FileNew()
    try:
        builder.TemplateFileName = SCRATCH_TEMPLATE
        builder.Units = tx.nx.Part.Units.Millimeters
        builder.NewFileName = str(Path(tx.workspace.root) / ".nx-mcp" / name)
        builder.DisplayPartOption = tx.nx.DisplayPartOption.AllowAdditional
        scratch = builder.Commit()
    finally:
        with contextlib.suppress(Exception):
            builder.Destroy()
    try:
        if not same_part(parts.Display, scratch):
            _set_display(parts, scratch)
        session.ApplicationSwitchImmediate(SHEET_METAL_APP)
        _set_display(parts, part)
        session.ApplicationSwitchImmediate(SHEET_METAL_APP)  # before the scratch part closes
    finally:
        scratch.Close(
            tx.nx.BasePart.CloseWholeTree.FalseValue,
            tx.nx.BasePart.CloseModified.CloseModified,
            None,
        )


def activate(tx, part, *, work: bool = True, display: bool = True) -> None:
    """Make ``part`` the display and/or work part (display first: NX moves work too).

    Never lets NX open its Sheet Metal application dialog (module notes).
    """
    parts = tx.session.Parts
    if display and parts.Display != part:
        if parts.Display is None and has_sheet_metal(part):
            _display_in_sheet_metal(tx, part)
        else:
            _set_display(parts, part)
    if display:
        keep_sheet_metal(tx)
    if work and parts.Work != part:
        parts.SetWork(part)


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


@contextlib.contextmanager
def saving(tx, part, *, force_display: bool = False):
    """Keep a part's drawing preview data through a save; restore the display after.

    Without drawing sheets, or when the part keeps no CGM preview data and
    ``force_display`` is False, nothing changes. A failed restore raises
    ``NX_RESTORE_FAILED`` (the save itself already happened).
    """
    sheets = list(getattr(part, "DrawingSheets", None) or [])
    keeps_cgm = bool(getattr(getattr(part, "SaveOptions", None), "DrawingCgmData", False))
    if not sheets or (not keeps_cgm and not force_display):
        yield
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
        yield
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

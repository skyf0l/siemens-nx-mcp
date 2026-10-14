"""``NXServices``: the kernel's services inside the NX process (architecture.md section 4).

One instance per host owns the session state the catalogue kernel needs: the
operation store (receipts), the object registry behind ``core.references``, part
generations, the agent's undo history and checkpoints, part ownership and the
engineer's grants, the agent's highlights, display snapshots (``restore_id``),
the review epoch and the link to the NX MCP panel.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from nx_mcp import side_effects
from nx_mcp.bridge import ObjectKind, ObjectRegistry
from nx_mcp.core import busy, catalogue
from nx_mcp.core import marks as undo_marks
from nx_mcp.core.errors import fail
from nx_mcp.core.receipts import OperationStore
from nx_mcp.core.references import PREFIX, Counter, References, Row
from nx_mcp.core.tx import Services
from nx_mcp.kit import builders, display, drawing, expr, units
from nx_mcp.kit import parts as kit_parts
from nx_mcp.kit import sketch as kit_sketch

APPLICATIONS = {
    "UG_APP_MODELING": "modeling",
    "UG_APP_SBSM": "sheet_metal",
    "UG_APP_DRAFTING": "drafting",
}
_KIND_BY_CLASS = (
    ("Features.Feature", "feature"),
    ("Body", "body"),
    ("Face", "face"),
    ("Edge", "edge"),
    ("Sketch", "sketch"),
    ("DatumPlane", "datum_plane"),
    ("DatumAxis", "datum_axis"),
    ("CoordinateSystem", "datum_csys"),
    ("Point", "point"),
    ("Assemblies.Component", "component"),
    ("Expression", "expression"),
    ("Drawings.DrawingSheet", "drawing_sheet"),
    ("Drawings.DraftingView", "drawing_view"),
    ("Part", "part"),
)
# Annotation collections a part may hold (some NX builds cannot enumerate all).
_ANNOTATION_GROUPS = (
    "Datums",
    "Fcfs",
    "DraftingDatumTargets",
    "DraftingImages",
    "IdSymbols",
    "PartsLists",
    "BendTables",
    "HoleTables",
    "TableSections",
    "Centerlines",
    "DraftingSurfaceFinishSymbol",
    "Welds",
)
# Rendering styles a restored camera puts back (display.STYLES names).
_RESTORED_STYLES = ("shaded", "shaded_with_edges", "wireframe")


def _path_key(part) -> str:
    """A part's file identity for ownership: its full path, else its name."""
    full = str(getattr(part, "FullPath", "") or getattr(part, "Name", "") or "")
    return full.replace("\\", "/").casefold()


def _name(value, fallback: str) -> str:
    return str(getattr(value, "Name", "") or getattr(value, "JournalIdentifier", "") or fallback)


def _tags(group) -> set:
    with contextlib.suppress(Exception):  # A collection this NX build cannot enumerate.
        return {int(o.Tag) for o in group or []}
    return set()


def _table_tags(part, uf) -> tuple[set, set]:
    """``(tables, inner)`` tags of ``part``: one tag per drawing table (parts list, bend
    or hole table, tabular note, title block), and the table sections that belong to a
    parts list, bend or hole table (NX lists them in ``TableSections`` too). NX's
    ``Annotations.Tables`` (the cells behind each table) is never read."""
    annotations = getattr(part, "Annotations", None)
    owners: set = set()
    for name in ("PartsLists", "BendTables", "HoleTables"):
        owners |= _tags(getattr(annotations, name, None))
    inner: set = set()
    sections = _tags(getattr(annotations, "TableSections", None))
    for tag in sections if owners and uf is not None else ():
        with contextlib.suppress(Exception):
            if int(uf.Tabnot.AskTabularNoteOfSection(tag)) in owners:
                inner.add(tag)
    blocks = _tags(getattr(getattr(part, "DraftingManager", None), "TitleBlocks", None))
    return owners | (sections - inner) | blocks, inner


def part_annotations(part) -> list:
    """Notes, labels, GD&T, tables, title blocks... of ``part``, one per tag."""
    values = [*getattr(part, "Notes", []), *getattr(part, "Labels", [])]
    with contextlib.suppress(Exception):  # drafting GD&T frames and datum symbols (NX 2406)
        values.extend(getattr(part, "Gdts", None) or [])
    group = getattr(part, "Annotations", None)
    if group is not None:
        for name in _ANNOTATION_GROUPS:
            with contextlib.suppress(Exception):  # A collection this NX build cannot enumerate.
                values.extend(getattr(group, name, None) or [])
    values.extend(getattr(getattr(part, "DraftingManager", None), "TitleBlocks", []))
    return list({int(obj.Tag): obj for obj in values}.values())


def components(part) -> list:
    """Every component under the root of ``part``, depth first."""
    root = part.ComponentAssembly.RootComponent

    def walk(parent):
        for child in parent.GetChildren():
            yield child
            yield from walk(child)

    return list(walk(root)) if root else []


def _component_patterns(part) -> list:
    assembly = part.ComponentAssembly
    if not assembly.RootComponent or not hasattr(assembly, "ComponentPatterns"):
        return []
    return list(assembly.ComponentPatterns.GetAllComponentPatterns())


def _assembly_constraints(part) -> list:
    positioner = getattr(getattr(part, "ComponentAssembly", None), "Positioner", None)
    return list(positioner.Constraints) if positioner is not None else []


def _modeling_views(part) -> list:
    views = getattr(part, "ModelingViews", [])
    return list(views) if hasattr(views, "__iter__") else []


class NXServices(Services):
    """Implements ``nx_mcp.core.tx.Services`` over one live NX session."""

    def __init__(self, session, nx, build: str, workspace) -> None:
        self.session = session
        self.nx = nx
        self.build = build
        self.workspace = workspace
        self.session_id = uuid.uuid4().hex
        self.objects = ObjectRegistry()  # opaque obj_ IDs behind the short refs
        self.objects.session_id = self.session_id
        self.store = OperationStore(workspace.root)
        self.store.recover(self.session_id)
        self.host: Any = None  # the interactive host (NX MCP panel), if any
        self.dev_tools: dict = {}  # development-only bridge methods (nx_dev_python)
        self.history: list = []  # the agent's committed model calls (nx_undo_operation)
        self.checkpoints: dict = {}
        self.highlighted: list = []  # objects the agent highlighted
        self.epoch = 0  # review epoch (``revision``)
        # Gate 6 state: part key -> file identity. A key alone could be reused by NX
        # for another part after an out-of-band close; the path must match too.
        self.owned_parts: dict = {}  # created or opened by the agent
        self.granted_parts: dict = {}  # allowed by the engineer on the panel
        self._generations: dict = {}  # part tag -> generation of this session
        # Registry entries a commit retired (consumed faces, deleted features): an undo
        # that brings the same NX object back (same tag) revives the handle and its refs.
        self._retired: dict = {}
        # Registry handle -> (journal id, NX name) when registered (fast snapshots).
        self._names: dict[str, tuple[str, str]] = {}
        state = Path(workspace.root) / ".nx-mcp" / "refs.json"
        live = Path(workspace.root) / ".nx-mcp" / "refs-live.json"  # across a host reload
        self.references = References(self, Counter(state), live_path=live)

    # -- plain members ---------------------------------------------------------
    @property
    def uf(self):
        module = getattr(self.nx, "UF", None) or importlib.import_module(self.nx.__name__ + ".UF")
        return module.UFSession.GetUFSession()

    @property
    def dev(self) -> bool:  # type: ignore[override]  # re-read from NX_MCP_DEV on each access
        return os.environ.get("NX_MCP_DEV") == "1"

    # -- parts and units -----------------------------------------------------------
    def work_part(self):
        """The part a read is bound to (gate 4s), else NX's work part."""
        if self.bound is not None:
            return self.bound
        return getattr(self.session.Parts, "Work", None)

    def switch(self, part, mode: str, *, application: str | None = None):
        """Gate 4s: make ``part`` the work part (``work``) or the displayed part
        (``display``) for one call (``kit.switching``); the entered switch."""
        from nx_mcp.kit import switching

        tx = SimpleNamespace(session=self.session, nx=self.nx, uf=self.uf, workspace=self.workspace)
        return switching.Switch(tx, part, mode, application=application).enter()

    def parts(self) -> list:
        return list(self.session.Parts)

    def shown_parts(self) -> tuple:
        """``(display part, work part)`` now, for :meth:`show_parts` after a failure."""
        parts = self.session.Parts
        return getattr(parts, "Display", None), getattr(parts, "Work", None)

    def show_parts(self, shown: tuple) -> None:
        """Make the parts of :meth:`shown_parts` the display and work parts again.

        A failed development snippet may have changed them; the display goes first (NX
        moves the work part with it). Changing the display part while an application
        runs keeps that application, so NX shows no dialog. Raises when NX refuses."""
        display, work = shown
        parts = self.session.Parts

        def same(a, b) -> bool:
            return a is not None and b is not None and int(a.Tag) == int(b.Tag)

        if display is not None and not same(getattr(parts, "Display", None), display):
            kit_parts.show(self.nx, parts, display)  # keeps NX's undo marks
        if work is not None and not same(getattr(parts, "Work", None), work):
            parts.SetWork(work)

    def part_key(self, part) -> str:
        tag = int(part.Tag)
        if tag not in self._generations:
            self._generations[tag] = uuid.uuid4().hex
        return "part_" + self.session_id + "_" + self._generations[tag]

    def part_path(self, part) -> str:
        full = str(getattr(part, "FullPath", "") or "")
        if not full:
            return str(getattr(part, "Name", "") or "part")
        try:
            return Path(full).resolve().relative_to(self.workspace.root).as_posix()
        except (ValueError, OSError):
            return full.replace("\\", "/")

    def owning_part(self, obj):
        if self.infer_kind(obj, default=None) == "part":
            return obj
        return getattr(obj, "OwningPart", None) or self.work_part()

    def part_units(self, part) -> str:
        units = getattr(part, "PartUnits", None)
        return "mm" if units == self.nx.BasePart.Units.Millimeters else "in"

    # -- drawing sheets (gate 9) ------------------------------------------------------
    def current_sheet(self):
        part = self.work_part()
        return drawing.current(part) if part is not None else None

    def sheet_of(self, obj):
        kind = self.infer_kind(obj, default=None)
        if kind == "drawing_sheet":
            return obj
        if kind != "drawing_view":
            # Annotations, dimensions and tables: the drawing domain's locators.
            for locate in catalogue.hooks("sheet").values():
                with contextlib.suppress(Exception):
                    sheet = locate(self, obj)
                    if sheet is not None:
                        return sheet
            return None
        matches = drawing.sheets_showing(self.owning_part(obj), obj)
        return matches[0] if len(matches) == 1 else None

    def sheet_units(self, sheet) -> str:
        return units.sheet_units(self, sheet)  # needs only ``.nx``

    def evaluate_formula(self, formula, dim, unit, *, field=None, scratch=False) -> float:
        return expr.evaluate(self, formula, dim, unit, field=field, scratch=scratch)

    def infer_kind(self, obj, default: str | None = "error"):
        for path, kind in _KIND_BY_CLASS:
            cls = self.nx
            for name in path.split("."):
                cls = getattr(cls, name, None)
                if cls is None:
                    break
            if isinstance(cls, type) and isinstance(obj, cls):
                return kind
        if default == "error":
            fail("NX_ERROR", f"Cannot infer the reference kind of {type(obj).__name__}")
        return default

    # -- object registry (opaque obj_ IDs behind core.references) ---------------------
    def register(self, obj, kind: str, part, fallback: str = "") -> dict:
        """Register ``obj`` of ``part``; its registry row (``id`` is the obj_ ID).

        ``kind`` is a registry kind: a snapshot group, or a reference kind mapped by
        ``core.references.TO_REGISTRY``."""
        key = self.part_key(part)
        raw_jid, raw_name = getattr(obj, "JournalIdentifier", ""), getattr(obj, "Name", "")
        journal_id, display_name = str(raw_jid), str(raw_name)
        reference = self.objects.register(
            obj,
            kind=cast(ObjectKind, kind),
            name=str(raw_name or raw_jid or fallback),
            part_id=key,
        )
        names = self._names
        names[reference.id] = (journal_id, display_name)
        if len(names) > 2 * len(self.objects._objects) + 20_000:  # forgotten handles
            kept = self.objects._objects.keys() | self._retired.keys()
            self._names = {h: v for h, v in names.items() if h in kept}
        return {
            "id": reference.id, "kind": reference.kind, "name": reference.name,
            "part_id": reference.part_id, "session_id": self.session_id,
            "generation_id": self._generations[int(part.Tag)],
            "owner_part_path": part.FullPath, "journal_id": journal_id,
            "display_name": display_name,
        }  # fmt: skip

    def register_handle(self, obj, kind: str, part) -> str:
        return self.register(obj, kind, part, kind.replace("_", " ").title())["id"]

    def resolve_handle(self, handle: str):
        return self.objects.resolve(handle)

    def handle_live(self, handle: str) -> bool:
        return handle in self.objects._objects

    def handle_kind(self, handle: str):
        entry = self.objects._objects.get(handle)
        return entry.reference.kind if entry is not None else None

    def handle_name(self, handle: str):
        return self.objects.display_name(handle)

    def object_of_tag(self, tag: int):
        """The NX object with ``tag``, or None (re-binding refs after a host reload)."""
        try:
            manager = getattr(self.nx, "TaggedObjectManager", None)
            return manager.GetTaggedObject(int(tag)) if manager is not None else None
        except Exception:
            return None

    def resolve(self, ref: str, kinds=()):
        """The live object of a short ref or obj_ ID (development snippets)."""
        return self.references.resolve(ref, kinds)

    # -- state for requires and ownership -----------------------------------------------
    def state(self) -> dict:
        part = self.work_part()
        state: dict[str, bool | str] = {
            "work_part": part is not None,
            "interactive": not self.session.IsBatch,
        }
        with contextlib.suppress(Exception):
            state["active_sketch"] = self.session.ActiveSketch is not None
        with contextlib.suppress(Exception):
            sheets = getattr(part, "DrawingSheets", None)
            state["drawing_sheet"] = getattr(sheets, "CurrentDrawingSheet", None) is not None
        with contextlib.suppress(Exception):
            state["assembly"] = part.ComponentAssembly.RootComponent is not None
        with contextlib.suppress(Exception):
            name = str(self.session.ApplicationName or "")
            state["application"] = APPLICATIONS.get(name, name.lower())
        return state

    def release_drafting_sketch(self) -> bool:
        """Gate 4 ``no_active_sketch``: deactivate an active drafting (sheet) sketch that
        NX re-activated on opening a sheet (``kit.sketch.release_drafting_sketch``); a
        model sketch stays active. Returns whether one was deactivated."""
        return kit_sketch.release_drafting_sketch(self.session, self.nx)

    def _held(self, table: dict, part) -> bool:
        return table.get(self.part_key(part)) == _path_key(part)

    def owns(self, part) -> bool:
        """Created or opened by the agent this session, or granted by the engineer."""
        return self._held(self.owned_parts, part) or self._held(self.granted_parts, part)

    def created(self, part) -> bool:
        """Created or opened by the agent (not only granted)."""
        return self._held(self.owned_parts, part)

    def granted(self, part) -> bool:
        return self._held(self.granted_parts, part)

    def own(self, part) -> None:
        self.owned_parts[self.part_key(part)] = _path_key(part)

    def grant(self, part) -> None:
        """The engineer allowed the agent to change ``part`` (NX MCP panel)."""
        self.granted_parts[self.part_key(part)] = _path_key(part)

    def revoke(self, part) -> None:
        self.granted_parts.pop(self.part_key(part), None)

    def loaded(self) -> dict:
        """``{part_key: file identity}`` of every loaded part (input of :meth:`track`)."""
        out = {}
        with contextlib.suppress(Exception):
            for part in self.parts():
                with contextlib.suppress(Exception):
                    out[self.part_key(part)] = _path_key(part)
        return out

    def track(self, before: dict) -> None:
        """After an agent call: own every part loaded since ``before`` (the agent created
        or opened it), and keep owning an agent part the call saved under a new path."""
        owned, granted = self.owned_parts, self.granted_parts
        try:
            parts = self.parts()
        except Exception:
            return
        for part in parts:
            with contextlib.suppress(Exception):
                key, now = self.part_key(part), _path_key(part)
                if key not in before:
                    owned[key] = now
                elif before[key] != now and before[key] in (owned.get(key), granted.get(key)):
                    owned[key] = now  # the agent saved its (or a granted) part as a new file
        busy.persist(self)  # the agent's parts survive a host reload (core/busy.py)
        self.references.persist()  # and so do its refs (core/references.py)

    def modified_flags(self) -> dict:
        out = {}
        with contextlib.suppress(Exception):
            for part in self.parts():
                with contextlib.suppress(Exception):
                    if not self.owns(part):
                        out[self.part_key(part)] = bool(part.IsModified)
        return out

    def quiet(self, calm=None):
        """No NX prompt while a call runs (host/quiet.py): update failures undo instead
        of asking, listing-window output goes to ``calm.listing``."""
        from nx_mcp.host import quiet

        return quiet.quiet(self.session, self.nx, Path(self.workspace.root) / ".nx-mcp", calm)

    # -- undo, snapshots, invalidation ----------------------------------------------------
    @contextlib.contextmanager
    def observe(self):
        observer = side_effects.SideEffectObserver().start()
        try:
            yield observer
        finally:
            observer.stop()

    def set_mark(self, name: str, visible: bool = True):
        visibility = self.nx.Session.MarkVisibility
        return self.session.SetUndoMark(
            visibility.Visible if visible else visibility.Invisible, name
        )

    def newest_mark(self):
        """NX's newest undo mark of any visibility (a development snippet that changed
        nothing leaves its own mark newest: ``dispatch.run_dev_tool``)."""
        return self.session.GetNewestUndoMark(self.nx.Session.MarkVisibility.AnyVisibility)

    def undo_to(self, mark) -> None:
        self.session.UndoToMark(mark, None)

    def delete_mark(self, mark) -> None:
        self.session.DeleteUndoMark(mark, None)

    def snapshot(self, part) -> dict:
        """``{(kind, tag): registry row}`` of every object of ``part`` a call may create
        or delete. Drawing tables (parts lists, bend tables, tabular notes, title
        blocks) are annotations whose rows say ``drawing_table``."""
        groups = [
            ("explosion", list(getattr(part.ComponentAssembly, "Explosions", []))),
            ("modeling_view", _modeling_views(part)),
            ("assembly_constraint", _assembly_constraints(part)),
            ("drawing_sheet", getattr(part, "DrawingSheets", [])),
            ("drawing_view", getattr(part, "DraftingViews", [])),
            ("dimension", getattr(part, "Dimensions", [])),
            ("annotation", part_annotations(part)),
            ("traceline", getattr(part, "Tracelines", [])),
            ("component_pattern", _component_patterns(part)),
            ("expression", getattr(part, "Expressions", [])),
            ("body", part.Bodies),
            ("feature", part.Features),
            ("curve", part.Curves),
            ("point", getattr(part, "Points", [])),
            ("sketch", part.Sketches),
            ("section", getattr(part, "DynamicSections", [])),
            ("component", components(part)),
        ]
        uf = None
        with contextlib.suppress(Exception):
            uf = self.uf
        tables, inner = _table_tags(part, uf)
        known = self._snapshot_rows(part)
        snap = {}
        for kind, values in groups:
            for v in values:
                tag = int(v.Tag)
                if kind == "annotation" and tag in inner:
                    continue
                snap[(kind, tag)] = known(v, kind, tag)
        for (kind, tag), entry in snap.items():
            if kind == "annotation" and tag in tables:
                entry["kind"] = "drawing_table"
        return snap

    def _snapshot_rows(self, part):
        """``row(obj, kind, tag)`` for :meth:`snapshot`: an object the registry already
        has costs no NX call (its row is rebuilt from the registry and the names read
        when it was registered); a new one is registered (:meth:`register`). A snapshot
        before and after every model call thus reads only the tags of a large part."""
        key = self.part_key(part)
        generation = self._generations[int(part.Tag)]
        path = part.FullPath
        objects = self.objects

        def row(obj, kind: str, tag: int) -> dict:
            handle = objects._identities.get((key, kind, str(tag)))
            entry = objects._objects.get(handle) if handle is not None else None
            if entry is None:
                return self.register(obj, kind, part, kind.title())
            entry.value = obj  # the live wrapper (ObjectRegistry.register does the same)
            journal_id, display_name = self._names.get(handle, ("", ""))
            reference = entry.reference
            return {
                "id": reference.id, "kind": reference.kind, "name": reference.name,
                "part_id": reference.part_id, "session_id": self.session_id,
                "generation_id": generation, "owner_part_path": path,
                "journal_id": journal_id, "display_name": display_name,
            }  # fmt: skip

        return row

    def describe(self, entry: dict, live: bool) -> Row:
        handle = entry.get("id")
        if live and handle is not None and self.handle_live(handle):
            obj = self.objects._objects[handle].value
            kind = entry.get("kind")
            if kind not in PREFIX or kind in ("datum", "section"):
                kind = self.infer_kind(obj, default=None) or {"section": "section_view"}.get(
                    kind or ""
                )
            if kind in PREFIX:
                return self.references.row(obj, kind)
        part_ref = None
        for ref, known in self.references._entries.items():
            if known.handle == handle:
                return {"id": ref, "kind": known.kind, "name": known.name, "part": known.part_id}
            if known.kind == "part" and known.part_key == entry.get("part_id"):
                part_ref = ref
        # A deleted object that never had a short ref: no id (a registry handle looks
        # like a ref but resolves to nothing), its part as the part's short ref.
        kind = entry.get("kind")
        row = {
            "kind": "section_view" if kind == "section" else kind,
            "name": entry.get("name") or entry.get("display_name") or "",
            "part": part_ref or entry.get("part_id"),
            "gone": True,
        }
        return cast(Row, row)  # an unknown object: its snapshot may lack kind or part

    def feature_errors(self, part, before: dict, mark) -> list:
        """New features of the call that report update errors (gate 11).

        NX 2406 hands the update error of a rolled-back feature to a feature created
        later (the internal datum of the next sketch, the next chamfer); updating that
        feature drops the stale message (:meth:`_drop_inherited_errors`). While a
        sketch is active NX does not update features, so a sketch created right after a
        rolled-back call kept the old error and failed once: the sketch is then
        deactivated with a model update and activated again before the errors are read.
        A real error survives the update and still fails the call.
        """
        self._drop_inherited_errors(part, before, mark)
        failed = self._new_feature_errors(part, before)
        sketch = None
        with contextlib.suppress(Exception):
            sketch = self.session.ActiveSketch
        if failed and sketch is not None and self._shown(sketch):
            reorient = self.nx.Sketch.ViewReorient.FalseValue
            try:
                sketch.Deactivate(reorient, self.nx.Sketch.UpdateLevel.Model)
            except Exception:
                return failed  # the update itself failed: report the errors as they are
            try:
                self._drop_inherited_errors(part, before, mark)
                failed = self._new_feature_errors(part, before)
            finally:
                sketch.Activate(reorient)
        return failed

    def _shown(self, sketch) -> bool:
        """Whether the sketch's part is displayed: NX 2406 faults activating a sketch
        elsewhere (``kit.sketch``), so the re-activation of :meth:`feature_errors` is
        skipped there."""
        try:
            return int(sketch.OwningPart.Tag) == int(self.session.Parts.Display.Tag)
        except Exception:
            return False

    def _drop_inherited_errors(self, part, before: dict, mark) -> None:
        """Update new features that report errors once, dropping inherited messages.

        NX 2406 keeps the error of a rolled-back feature and attaches it to the next
        feature created under the same name (for example CHAMFER(3)), although that
        feature built fine. Updating the feature drops the stale message; a real error
        remains.
        """
        manager = getattr(self.session, "UpdateManager", None)
        if manager is None or not hasattr(manager, "LogForUpdate"):
            return
        with contextlib.suppress(Exception):
            new = [
                f
                for f in part.Features
                if ("feature", int(f.Tag)) not in before
                and list(getattr(f, "GetFeatureErrorMessages", list)())
            ]
            if new:
                for feature in new:
                    manager.LogForUpdate(feature)
                manager.DoUpdate(mark)

    def _new_feature_errors(self, part, before: dict) -> list:
        failed = []
        for feature in part.Features:
            if ("feature", int(feature.Tag)) in before:
                continue
            messages = []
            with contextlib.suppress(Exception):
                messages = [str(m) for m in feature.GetFeatureErrorMessages()]
            if messages:
                failed.append(
                    {"feature": _name(feature, "Feature"), "message": "; ".join(messages)}
                )
        return failed

    def clear_update_errors(self) -> None:
        """Drop NX update errors left by a rolled-back commit: otherwise NX attaches the
        failed operation's message to the next feature, and model health reports an
        error that no longer exists."""
        manager = getattr(self.session, "UpdateManager", None)
        clear = getattr(manager, "ClearErrorList", None)
        if clear is not None:
            with contextlib.suppress(Exception):
                clear()

    def invalidate(self, before: dict, after: dict, topology: bool) -> set:
        """After a committed call: retire the handles of deleted objects and, for a
        topology change, of the faces and edges that NX no longer has.

        Refs follow NX's persistent object identity (the tag). NX 2406 keeps the tag of
        a face or edge a feature did not consume, also when the feature changed its
        geometry (measured live: the top face through a pocket, hole faces through four
        symbolic threads, every untouched edge through a blend): such refs stay live
        and name the same, possibly reshaped, face. A consumed face or edge leaves its
        body's face/edge list (its wrapper may keep a non-zero tag) and is retired.
        Returns the retired handles."""
        removed = {v["id"] for k, v in before.items() if k not in after}
        dead = self._dead_handles(None, kinds={"face", "edge"}) if topology else set()
        return self._retire(removed | dead)

    _RETIRED_KEPT = 5000  # retired entries an undo may revive (oldest dropped first)

    def _retire(self, handles) -> set:
        objects = self.objects
        retired = {h for h in handles if h in objects._objects}
        for handle in retired:
            self._retired[handle] = objects._objects.pop(handle)
            objects._stale_ids.add(handle)
        for handle in list(self._retired)[: max(0, len(self._retired) - self._RETIRED_KEPT)]:
            del self._retired[handle]
        if retired:
            objects._identities = {
                k: v for k, v in objects._identities.items() if v in objects._objects
            }
        return retired

    def invalidate_parts(self, parts) -> None:
        keys = {self.part_key(part) for part in parts}
        for key in keys:
            self.objects.invalidate_part(key)
        self._forget_retired(keys)

    def _forget_retired(self, keys) -> None:
        """No undo revives a handle of these parts any more."""
        self._retired = {h: e for h, e in self._retired.items() if e.reference.part_id not in keys}

    _UNDO_COLLECTIONS = {
        "body": "Bodies",
        "feature": "Features",
        "sketch": "Sketches",
        "curve": "Curves",
        "expression": "Expressions",
        "datum": "Datums",
    }

    def retire_undone(self, parts, revive: bool = True) -> set:
        """After an undo (or an engineer's edit): retire the handles of ``parts`` whose
        object did not come back.

        NX 2406 undo restores the same objects under the same tags (measured live for
        the part, bodies, features, sketches, faces and edges after UndoToMark of a
        committed unite, a feature delete and a feature edit). A handle stays live when
        its wrapper still has a non-zero tag that is again in its part's collection
        (faces and edges: of the part's bodies; occurrences: their prototype in its
        part); kinds without a collection only need the non-zero tag. ``revive``: also
        revive handles an earlier commit retired whose object came back (:meth:`_revive`)."""
        keys = set()
        for part in parts:
            with contextlib.suppress(Exception):
                keys.add(self.part_key(part))
        retired = self._retire(self._dead_handles(keys))
        if revive:
            self._revive(keys)
        return retired

    def _revive(self, part_keys: set) -> set:
        """Handles an earlier commit retired whose NX object an undo brought back: the
        same wrapper again has its non-zero tag in its part (NX 2406 undo restores the
        tag, which NX keeps reserved while the undo mark exists). Their refs work again;
        a handle whose object meanwhile got a new handle stays retired."""
        objects = self.objects
        dead = self._dead_handles(part_keys, entries=self._retired)
        revived = set()
        for handle, entry in list(self._retired.items()):
            ref = entry.reference
            if handle in dead or ref.part_id not in part_keys:
                continue
            key = (ref.part_id, ref.kind, str(int(entry.value.Tag)))
            if objects._identities.get(key) in objects._objects:
                continue
            objects._objects[handle] = self._retired.pop(handle)
            objects._stale_ids.discard(handle)
            objects._identities[key] = handle
            revived.add(handle)
        return revived

    def _dead_handles(
        self, part_keys: set | None, kinds: set | None = None, entries: dict | None = None
    ) -> set:
        """Handles (of ``part_keys``, of registry ``kinds``; None = all; of ``entries``,
        default the registry) whose object NX no longer has: tag 0, or a tag missing
        from its part's live collection."""
        by_key: dict = {}
        with contextlib.suppress(Exception):
            by_key = {self.part_key(p): p for p in self.parts()}
        live: dict = {}

        def known(part, kind):
            key = (int(part.Tag), kind)
            if key not in live:
                live[key] = self._undo_tags(part, kind)
            return live[key]

        def on_body(value, kind, part, tag) -> bool:
            """A face or edge lives while its body is one of the part's and still lists
            it: only the bodies that hold registered faces and edges are read, never every
            face of a large part."""
            get_body = getattr(value, "GetBody", None)
            if get_body is None:
                tags = known(part, kind)
                return tags is None or tag in tags
            body = get_body()  # raises for an object NX no longer has: dead
            if body is None:
                return False
            body_tag = int(body.Tag)
            bodies = known(part, "body")
            if bodies is not None and body_tag not in bodies:
                return False
            key = (body_tag, kind)
            if key not in live:
                method = "GetFaces" if kind == "face" else "GetEdges"
                live[key] = {int(x.Tag) for x in getattr(body, method)()}
            return tag in live[key]

        dead: set[str] = set()
        for handle, entry in list((self.objects._objects if entries is None else entries).items()):
            ref = entry.reference
            if part_keys is not None and ref.part_id not in part_keys:
                continue
            if kinds is not None and ref.kind not in kinds:
                continue
            try:
                value = entry.value
                tag = int(value.Tag)
                if tag == 0:
                    alive = False
                elif getattr(value, "IsOccurrence", False):
                    alive = self._occurrence_alive(value, ref.kind, known)
                elif ref.kind in ("face", "edge"):
                    part = by_key.get(ref.part_id)
                    alive = part is not None and on_body(value, ref.kind, part, tag)
                else:
                    part = by_key.get(ref.part_id)
                    tags = known(part, ref.kind) if part is not None else None
                    alive = part is not None and (tags is None or tag in tags)
            except Exception:
                alive = False
            if not alive:
                dead.add(handle)
        return dead

    def _occurrence_alive(self, value, kind: str, known) -> bool:
        """An occurrence face or edge lives while its prototype does; other occurrences
        (components, bodies) only need the non-zero tag."""
        if kind not in ("face", "edge"):
            return True
        prototype = value.Prototype
        tags = known(prototype.OwningPart, kind)
        return int(prototype.Tag) != 0 and (tags is None or int(prototype.Tag) in tags)

    def _undo_tags(self, part, kind: str) -> set | None:
        """Tags of ``part``'s objects of registry ``kind``; None when NX cannot list them
        (then only the wrapper's tag is checked)."""
        try:
            if kind in ("face", "edge"):
                method = "GetFaces" if kind == "face" else "GetEdges"
                return {int(x.Tag) for b in part.Bodies for x in getattr(b, method)()}
            if kind == "part":
                return {int(p.Tag) for p in self.parts()}
            name = self._UNDO_COLLECTIONS.get(kind)
            values = getattr(part, name, None) if name else None
            return None if values is None else {int(v.Tag) for v in values}
        except Exception:
            return None

    def forget_parts(self, keys) -> None:
        """Parts NX closed (``part_key`` taken before the close): drop their references,
        undo history, checkpoints and ownership, and retire the key so a reused NX tag
        never inherits them."""
        keys = set(keys)
        if not keys:
            return
        self._forget_retired(keys)
        for key in keys:
            self.objects.invalidate_part(key)
            self.owned_parts.pop(key, None)
            self.granted_parts.pop(key, None)
        for tag, generation in list(self._generations.items()):
            if any(key.endswith("_" + generation) for key in keys):
                self._generations.pop(tag, None)
        # A barrier stays: undo across the load still crosses the part's lifetime.
        undo_marks.remove_entries(
            self,
            [h for h in self.history if undo_marks.barrier(h) is None and h.get("part_id") in keys],
        )
        redo = self.redo_steps()
        redo[:] = [h for h in redo if h.get("part_id") not in keys]
        for cp in self.checkpoints.values():  # listed as expired, never dropped silently
            if cp.get("part_id") in keys and cp.get("expired") in (None, "mark_evicted"):
                cp["expired"] = "part_closed"  # what matters more than an evicted mark

    def engineer_changed(self, display_changed: bool = False) -> None:
        """The engineer edited the model between agent calls: an ``engineer`` barrier in
        the undo history (``core/marks.engineer_change``) keeps every agent undo and
        restore of every part from reaching back over the edit, so none reverts it; the
        history before it stays listed and checkpoints stay listed, expired. References
        are re-validated by the host (:meth:`revalidate`), not dropped. Nothing is left to
        redo either: the undone calls were made for the model before the edit.

        ``display_changed``: the engineer also switched the display part, which drops
        NX's undo marks (a plain display switch): entries and checkpoints whose mark went
        say ``display_reset``."""
        undo_marks.engineer_change(self, display_changed)
        self.epoch += 1

    def revalidate(self) -> dict:
        """After an edit the agent did not make (the engineer's, or NX's own): keep the
        refs of every object that is still in its part, re-bind anchored refs by
        anchor, retire the rest. Part refs of loaded parts always stay."""
        retired = self.retire_undone(self.parts(), revive=False)  # no undo: revive nothing
        return self.references.rebind(retired)

    def refreshers(self) -> dict:
        return dict(catalogue.hooks("refresher"))

    # -- display state (view rollback, restore_id) ---------------------------------------
    def display_camera(self) -> dict:
        part = self.session.Parts.Display
        if part is None:
            fail(
                "NX_NO_WORK_PART", "Open a display part first", hint="Open a part with nx_open_part"
            )
        return display.camera(self, part.ModelingViews.WorkView)

    def display_records(self, objects, appearance: bool) -> list:
        """``[(obj, record)]`` of display state, one record per object given: with
        ``appearance`` a body's record carries its colour, the transparency most of its
        faces have and only the faces that differ (``display.body_appearance``); faces
        get records of their own only when they are given."""
        values = list({int(x.Tag): x for x in objects}.values())
        if len(values) > display.MAX_RECORDS:
            fail(
                "NX_TOO_MANY_OBJECTS",
                f"A display change takes at most {display.MAX_RECORDS} objects",
                hint="Pass bodies or components rather than their faces, or fewer objects",
            )
        return [(x, self._display_record(x, appearance)) for x in values]

    def _display_record(self, obj, appearance: bool) -> dict:
        nx = self.nx
        datum = tuple(
            getattr(nx, n)
            for n in ("DatumPlane", "DatumAxis", "CoordinateSystem")
            if hasattr(nx, n)
        )
        if isinstance(obj, datum):
            kind = "datum"
        elif hasattr(obj, "FindOccurrence"):
            kind = "component"
        elif isinstance(obj, nx.Body):
            kind = "body"
        elif isinstance(obj, nx.Face):
            kind = "face"
        elif isinstance(obj, nx.Edge):
            kind = "edge"
        else:
            kind = "curve"
        part = self.work_part()
        if part is None:
            fail("NX_NO_WORK_PART", "No work part is open", hint="Open a part with nx_open_part")
        record = {
            "object": self.register(obj, kind, part, "Display object"),
            "blanked": bool(obj.IsBlanked),
        }
        if appearance and kind == "body":
            record.update(
                display.body_appearance(
                    obj,
                    list(obj.GetFaces()),
                    lambda face: self.uf.Obj.AskTranslucency(face.Tag),
                    lambda face: self.register(face, "face", part, "Display object"),
                )
            )
        elif appearance:
            record["color_index"] = obj.Color
            if isinstance(obj, nx.Face):
                record["transparency"] = self.uf.Obj.AskTranslucency(obj.Tag)
        return record

    def _display_face(self, row: dict):
        """The face a body record lists (``display.body_appearance``), None when gone."""
        try:
            return self.resolve_handle(str(display.record_id(row)))
        except Exception:  # noqa: BLE001 - a face the model no longer has is skipped
            return None

    def highlights(self) -> list:
        return list(self.highlighted)

    def set_highlights(self, objects) -> None:
        """Make the agent's highlights exactly ``objects`` (restores a kept set)."""
        wanted = {int(o.Tag): o for o in objects}
        for obj in list(self.highlighted):
            if int(obj.Tag) not in wanted:
                with contextlib.suppress(Exception):  # NX may have deleted it since.
                    obj.Unhighlight()
        current = {int(o.Tag) for o in self.highlighted}
        for tag, obj in wanted.items():
            if tag not in current:
                obj.Highlight()
        self.highlighted = list(wanted.values())

    def clear_highlights(self) -> int:
        """Remove the agent's highlights (never the engineer's); how many were cleared."""
        cleared = 0
        for obj in self.highlighted:
            try:
                obj.Unhighlight()
                cleared += 1
            except Exception:
                pass  # NX may have deleted an entity since the highlight.
        self.highlighted = []
        return cleared

    def restore_display(self, camera, records, highlights) -> None:
        for obj, record in records:
            if "color_index" in record:
                display.apply_appearance(
                    self, [obj], record["color_index"], record.get("transparency")
                )
                display.restore_faces(self, record, self._display_face)
            (obj.Blank if record["blanked"] else obj.Unblank)()
        if highlights is not None:
            self.set_highlights(highlights)
        if camera is not None:
            self._restore_camera(camera)

    def _restore_camera(self, camera: dict) -> None:
        part = self.work_part()
        if part is None or part != self.session.Parts.Display:
            fail(
                "NX_WRONG_PART_CONTEXT",
                "Activate the target as both work and display part",
                hint="Use nx_activate_part",
            )
        view = part.ModelingViews.WorkView
        view.SetRotationTranslationScale(
            builders.matrix(self, camera["rotation"]),
            self.nx.Point3d(*(float(v) for v in camera["origin"])),
            float(camera["scale"]),
        )
        view.UpdateDisplay()
        if camera.get("style") in _RESTORED_STYLES:
            display.set_style(self, view, camera["style"])

    def save_display_snapshot(self, records, camera) -> str:
        part = self.work_part()
        if part is None:
            fail("NX_NO_WORK_PART", "No work part is open", hint="Open a part with nx_open_part")
        return display.snapshots.save(self.part_key(part), [r for _, r in records], camera)

    # -- the agent's undo history -----------------------------------------------------
    def record_history(self, mark, part, operation_id: str, tool: str) -> None:
        if not self.mark_exists(mark):  # an undo or restore went back past its own mark
            return
        self.history.append(
            {
                "mark": mark,
                "part_id": self.part_key(part) if part is not None else None,
                "operation_id": operation_id,
                "method": tool,
            }
        )

    def undo_history(self) -> list:
        """The live history: the host clears it on engineer changes."""
        return self.history

    def undo_checkpoints(self) -> dict:
        return self.checkpoints

    def mark_exists(self, mark) -> bool:
        try:
            return bool(self.session.DoesUndoMarkExist(mark, None))
        except Exception:
            return mark is not None

    # -- the NX MCP panel (nx_get_status) --------------------------------------
    def control_status(self) -> dict | None:
        return None if self.host is None else self.host.status()

    def revision(self) -> int:
        return self.epoch

    def after_commit(self, spec) -> None:
        if spec.effect != "read":
            self.epoch += 1

    def after_failure(self, spec, outcome: str | None = None) -> None:
        """A failed call moves the review epoch only when it may have changed something:
        a refusal (``not_started``) or a failed read changed nothing."""
        if outcome not in ("not_started", "not_applicable"):
            self.after_commit(spec)

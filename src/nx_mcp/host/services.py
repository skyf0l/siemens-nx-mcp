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
from typing import Any, cast

from nx_mcp import side_effects
from nx_mcp.bridge import ObjectKind, ObjectRegistry
from nx_mcp.core import catalogue
from nx_mcp.core.errors import fail
from nx_mcp.core.receipts import OperationStore
from nx_mcp.core.references import PREFIX, Counter, References, Row
from nx_mcp.core.tx import Services
from nx_mcp.kit import builders, display, drawing, units

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
    "IdSymbols",
    "PartsLists",
    "BendTables",
    "TableSections",
    "Tables",
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


def _table_tags(part) -> set:
    """Tags of the drawing tables of ``part`` (empty when it has none or no drafting)."""
    tags: set = set()
    annotations = getattr(part, "Annotations", None)
    groups = [
        getattr(annotations, n, None)
        for n in ("PartsLists", "BendTables", "TableSections", "Tables")
    ]
    groups.append(getattr(getattr(part, "DraftingManager", None), "TitleBlocks", None))
    for group in groups:
        with contextlib.suppress(Exception):
            tags.update(int(o.Tag) for o in group or [])
    return tags


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
        state = Path(workspace.root) / ".nx-mcp" / "refs.json"
        self.references = References(self, Counter(state))

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
        return getattr(self.session.Parts, "Work", None)

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
            _, status = parts.SetDisplay(display, False, False)
            if status is not None and hasattr(status, "Dispose"):
                with contextlib.suppress(Exception):
                    status.Dispose()
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
        ref: dict = self.objects.register(
            obj, kind=cast(ObjectKind, kind), name=_name(obj, fallback), part_id=key
        ).model_dump()
        ref.update(
            session_id=self.session_id,
            generation_id=self._generations[int(part.Tag)],
            owner_part_path=part.FullPath,
            journal_id=str(getattr(obj, "JournalIdentifier", "")),
            display_name=str(getattr(obj, "Name", "")),
        )
        return ref

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
        snap = {
            (kind, int(v.Tag)): self.register(v, kind, part, kind.title())
            for kind, values in groups
            for v in values
        }
        tables = _table_tags(part)
        for (kind, tag), entry in snap.items():
            if kind == "annotation" and tag in tables:
                entry["kind"] = "drawing_table"
        return snap

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
        for ref, known in self.references._entries.items():
            if known.handle == handle:
                return {"id": ref, "kind": known.kind, "name": known.name, "part": known.part_id}
        row = {
            "id": handle,
            "kind": entry.get("kind"),
            "name": entry.get("name") or entry.get("display_name") or "",
            "part": entry.get("part_id"),
        }
        return cast(Row, row)  # an unknown object: its snapshot may lack id, kind or part

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
        if failed and sketch is not None:
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

    def invalidate(self, before: dict, after: dict, topology: bool) -> None:
        """Retire the IDs of deleted objects (and of every face and edge for a
        topology change)."""
        objects = self.objects
        removed = {v["id"] for k, v in before.items() if k not in after}
        for key, entry in list(objects._objects.items()):
            if key in removed or (topology and entry.reference.kind in {"face", "edge"}):
                objects._objects.pop(key, None)
                objects._stale_ids.add(key)
        objects._identities = {
            k: v for k, v in objects._identities.items() if v in objects._objects
        }

    def invalidate_parts(self, parts) -> None:
        for part in parts:
            self.objects.invalidate_part(self.part_key(part))

    _UNDO_COLLECTIONS = {
        "body": "Bodies",
        "feature": "Features",
        "sketch": "Sketches",
        "curve": "Curves",
        "expression": "Expressions",
        "datum": "Datums",
    }

    def retire_undone(self, parts) -> set:
        """After an undo: retire the handles of ``parts`` whose object did not come back.

        NX 2406 undo restores the same objects under the same tags (measured live for
        the part, bodies, features, sketches, faces and edges after UndoToMark of a
        committed unite, a feature delete and a feature edit). A handle stays live when
        its wrapper still has a non-zero tag that is again in its part's collection
        (faces and edges: of the part's bodies); kinds without a collection, and
        occurrences, only need the non-zero tag."""
        objects = self.objects
        retired: set[str] = set()
        for part in parts:
            try:
                key = self.part_key(part)
            except Exception:
                continue
            live: dict[str, set | None] = {}
            for handle, entry in list(objects._objects.items()):
                if entry.reference.part_id != key:
                    continue
                kind = entry.reference.kind
                try:
                    tag = int(entry.value.Tag)
                    if getattr(entry.value, "IsOccurrence", False):
                        known = None
                    else:
                        if kind not in live:
                            live[kind] = self._undo_tags(part, kind)
                        known = live[kind]
                    alive = tag != 0 and (known is None or tag in known)
                except Exception:
                    alive = False
                if not alive:
                    retired.add(handle)
        for handle in retired:
            objects._objects.pop(handle, None)
            objects._stale_ids.add(handle)
        if retired:
            objects._identities = {
                k: v for k, v in objects._identities.items() if v in objects._objects
            }
        return retired

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
        for key in keys:
            self.objects.invalidate_part(key)
            self.owned_parts.pop(key, None)
            self.granted_parts.pop(key, None)
        for tag, generation in list(self._generations.items()):
            if any(key.endswith("_" + generation) for key in keys):
                self._generations.pop(tag, None)
        self.history[:] = [h for h in self.history if h.get("part_id") not in keys]
        for name in [k for k, v in self.checkpoints.items() if v.get("part_id") in keys]:
            del self.checkpoints[name]

    def engineer_changed(self) -> None:
        """The engineer edited the model between agent calls: no reference resolves
        across the edit and no undo or checkpoint reaches back over it."""
        self.history.clear()
        self.checkpoints.clear()
        self.epoch += 1

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
        """``[(obj, record)]`` of display state; bodies expand to faces with appearance."""
        values = list(objects)
        if appearance:
            values = list(
                {
                    int(x.Tag): x
                    for obj in values
                    for x in (
                        [obj] + list(obj.GetFaces()) if isinstance(obj, self.nx.Body) else [obj]
                    )
                }.values()
            )
        if len(values) > display.MAX_RECORDS:
            fail("NX_TOO_MANY_OBJECTS", "Display change exceeds 10000 objects/faces")
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
        if appearance:
            record["color_index"] = obj.Color
            if isinstance(obj, nx.Face):
                record["transparency"] = self.uf.Obj.AskTranslucency(obj.Tag)
        return record

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

    def after_failure(self, spec) -> None:
        self.after_commit(spec)

"""``LegacyServices``: the kernel's services over today's ``HardenedExecutor``.

Reuses the existing operation store, object registry, undo-mark history,
snapshots, update-error cleanup, annotation refresh and review epoch
(architecture.md section 4) without changing them. Step F4 replaces this module
with ``host/nxservices.py``.
"""

from __future__ import annotations

import contextlib
import importlib
import os
from pathlib import Path

from nx_mcp import side_effects
from nx_mcp.core import catalogue
from nx_mcp.core.references import PREFIX, Counter, References
from nx_mcp.kit import drawing, units

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


def _path_key(part) -> str:
    """A part's file identity for ownership: its full path, else its name."""
    full = str(getattr(part, "FullPath", "") or getattr(part, "Name", "") or "")
    return full.replace("\\", "/").casefold()


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


class LegacyServices:
    """Implements ``nx_mcp.core.tx.Services`` for one ``HardenedExecutor``."""

    def __init__(self, executor) -> None:
        self.x = executor
        # Gate 6 state: part key -> file identity. A key alone could be reused by NX
        # for another part after an out-of-band close; the path must match too.
        if not isinstance(getattr(executor, "_owned_parts", None), dict):
            executor._owned_parts = {}  # created or opened by the agent
        if not isinstance(getattr(executor, "_granted_parts", None), dict):
            executor._granted_parts = {}  # allowed by the engineer on the panel
        refs = getattr(executor, "_catalogue_references", None)
        if refs is None:
            state = Path(executor.workspace.root) / ".nx-mcp" / "refs.json"
            refs = References(self, Counter(state))
            executor._catalogue_references = refs
        refs.backend = self
        self.references = refs

    # -- plain members ---------------------------------------------------------
    @property
    def nx(self):
        return self.x.nxopen

    @property
    def uf(self):
        module = getattr(self.nx, "UF", None) or importlib.import_module(self.nx.__name__ + ".UF")
        return module.UFSession.GetUFSession()

    @property
    def session(self):
        return self.x.session

    @property
    def workspace(self):
        return self.x.workspace

    @property
    def build(self):
        return self.x.nx_version

    @property
    def dev(self) -> bool:
        return os.environ.get("NX_MCP_DEV") == "1"

    @property
    def session_id(self):
        return self.x.session_id

    @property
    def store(self):
        return self.x.store

    # -- parts and units -----------------------------------------------------------
    def work_part(self):
        return self.x._work_part(required=False)

    def parts(self) -> list:
        return list(self.x.session.Parts)

    def part_key(self, part) -> str:
        return self.x._part_id(part)

    def part_path(self, part) -> str:
        full = str(getattr(part, "FullPath", "") or "")
        if not full:
            return str(getattr(part, "Name", "") or "part")
        try:
            return Path(full).resolve().relative_to(self.x.workspace.root).as_posix()
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
            from nx_mcp.core.errors import fail

            fail("NX_ERROR", f"Cannot infer the reference kind of {type(obj).__name__}")
        return default

    # -- legacy registry seam --------------------------------------------------------
    def legacy_register(self, obj, kind: str, part) -> str:
        return self.x._reference(obj, kind, part, kind.replace("_", " ").title())["id"]

    def legacy_resolve(self, legacy_id: str):
        return self.x.objects.resolve(legacy_id)

    def legacy_live(self, legacy_id: str) -> bool:
        return legacy_id in self.x.objects._objects

    def legacy_kind(self, legacy_id: str):
        entry = self.x.objects._objects.get(legacy_id)
        return entry.reference.kind if entry is not None else None

    def legacy_name(self, legacy_id: str):
        return self.x.objects.display_name(legacy_id)

    # -- state for requires and ownership -----------------------------------------------
    def state(self) -> dict:
        part = self.work_part()
        state = {"work_part": part is not None, "interactive": not self.x.session.IsBatch}
        with contextlib.suppress(Exception):
            state["active_sketch"] = self.x.session.ActiveSketch is not None
        with contextlib.suppress(Exception):
            sheets = getattr(part, "DrawingSheets", None)
            state["drawing_sheet"] = getattr(sheets, "CurrentDrawingSheet", None) is not None
        with contextlib.suppress(Exception):
            state["assembly"] = part.ComponentAssembly.RootComponent is not None
        with contextlib.suppress(Exception):
            name = str(self.x.session.ApplicationName or "")
            state["application"] = APPLICATIONS.get(name, name.lower())
        return state

    def _held(self, table: dict, part) -> bool:
        return table.get(self.part_key(part)) == _path_key(part)

    def owns(self, part) -> bool:
        """Created or opened by the agent this session, or granted by the engineer."""
        return self._held(self.x._owned_parts, part) or self._held(self.x._granted_parts, part)

    def created(self, part) -> bool:
        """Created or opened by the agent (not only granted)."""
        return self._held(self.x._owned_parts, part)

    def granted(self, part) -> bool:
        return self._held(self.x._granted_parts, part)

    def own(self, part) -> None:
        self.x._owned_parts[self.part_key(part)] = _path_key(part)

    def grant(self, part) -> None:
        """The engineer allowed the agent to change ``part`` (NX MCP panel)."""
        self.x._granted_parts[self.part_key(part)] = _path_key(part)

    def revoke(self, part) -> None:
        self.x._granted_parts.pop(self.part_key(part), None)

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
        owned, granted = self.x._owned_parts, self.x._granted_parts
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
        mark = self.x.session.SetUndoMark(
            visibility.Visible if visible else visibility.Invisible, name
        )
        if visible:
            self.x._active_mark = mark
        return mark

    def undo_to(self, mark) -> None:
        self.x.session.UndoToMark(mark, None)

    def delete_mark(self, mark) -> None:
        self.x.session.DeleteUndoMark(mark, None)

    def snapshot(self, part) -> dict:
        """The legacy snapshot with exact kinds: it lists drawing tables (parts lists,
        bend tables, tabular notes, title blocks) among annotations; their entries say
        ``drawing_table`` so ``changes`` reports them as such (keys stay unchanged)."""
        snap = self.x._snapshot(part)
        tables = _table_tags(part)
        for (kind, tag), entry in snap.items():
            if kind == "annotation" and tag in tables and isinstance(entry, dict):
                entry["kind"] = "drawing_table"
        return snap

    def describe(self, entry: dict, live: bool) -> dict:
        legacy_id = entry.get("id")
        if live and self.legacy_live(legacy_id):
            obj = self.x.objects._objects[legacy_id].value
            kind = entry.get("kind")
            if kind not in PREFIX or kind in ("datum", "section"):
                kind = self.infer_kind(obj, default=None) or {"section": "section_view"}.get(kind)
            if kind in PREFIX:
                return self.references.row(obj, kind)
        for ref, known in self.references._entries.items():
            if known.legacy_id == legacy_id:
                return {"id": ref, "kind": known.kind, "name": known.name, "part": known.part_id}
        return {
            "id": legacy_id,
            "kind": entry.get("kind"),
            "name": entry.get("name") or entry.get("display_name") or "",
            "part": entry.get("part_id"),
        }

    def feature_errors(self, part, before: dict, mark) -> list:
        """New features of the call that report update errors (gate 11).

        NX 2406 hands the update error of a rolled-back feature to a feature created
        later (the internal datum of the next sketch, the next chamfer); updating that
        feature drops the stale message (``_drop_inherited_feature_errors``). While a
        sketch is active NX does not update features, so a sketch created right after a
        rolled-back call kept the old error and failed once: the sketch is then
        deactivated with a model update and activated again before the errors are read.
        A real error survives the update and still fails the call.
        """
        self.x._active_mark = mark
        self.x._drop_inherited_feature_errors(part, before)
        failed = self._new_feature_errors(part, before)
        sketch = None
        with contextlib.suppress(Exception):
            sketch = self.x.session.ActiveSketch
        if failed and sketch is not None:
            reorient = self.nx.Sketch.ViewReorient.FalseValue
            try:
                sketch.Deactivate(reorient, self.nx.Sketch.UpdateLevel.Model)
            except Exception:
                return failed  # the update itself failed: report the errors as they are
            try:
                self.x._drop_inherited_feature_errors(part, before)
                failed = self._new_feature_errors(part, before)
            finally:
                sketch.Activate(reorient)
        return failed

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
                    {"feature": self.x._name(feature, "Feature"), "message": "; ".join(messages)}
                )
        return failed

    def clear_update_errors(self) -> None:
        self.x._clear_update_errors()

    def invalidate(self, before: dict, after: dict, topology: bool) -> None:
        self.x._invalidate_deleted(before, after, invalidate_topology=topology)

    def invalidate_parts(self, parts) -> None:
        for part in parts:
            self.x.objects.invalidate_part(self.part_key(part))

    def forget_parts(self, keys) -> None:
        """Parts NX closed (``part_key`` taken before the close): drop their references,
        undo history, checkpoints and ownership, and retire the key so a reused NX tag
        never inherits them (the legacy ``_close_loaded_part`` bookkeeping)."""
        keys = set(keys)
        if not keys:
            return
        x = self.x
        for key in keys:
            x.objects.invalidate_part(key)
            x._owned_parts.pop(key, None)
            x._granted_parts.pop(key, None)
        generations = getattr(x, "_part_generations", {})
        for tag, generation in list(generations.items()):
            if any(key.endswith("_" + generation) for key in keys):
                generations.pop(tag, None)
        x._history = [h for h in getattr(x, "_history", []) if h.get("part_id") not in keys]
        x._checkpoints = {
            k: v for k, v in getattr(x, "_checkpoints", {}).items() if v.get("part_id") not in keys
        }

    def refresh_annotations(self, spec) -> list:
        """The legacy refresher, only where it still manages something the hooks do
        not: measured sheet-metal callouts. Detail-view boundaries and automatic bend
        tables are refreshed by the drawing domain's ``@refresher`` hooks (gate 13)."""
        refresh = getattr(self.x, "_refresh_annotations", None)
        part = self.work_part()
        if refresh is None or part is None:
            return []
        hooks = catalogue.hooks("refresher")
        covered = {"drawing_view", "drawing_table"} <= set(hooks)
        if covered and not self._measured_pmi(part):
            return []
        return list(refresh().get("updated") or [])

    def _measured_pmi(self, part) -> bool:
        """True when the part holds a legacy measured callout to refresh (or when that
        cannot be told: the legacy refresher then runs, as before)."""
        try:
            kind = self.nx.NXObject.AttributeType.String
        except AttributeError:
            return True
        for obj in self.x._documentation_annotations(part):
            with contextlib.suppress(Exception):
                if obj.HasUserAttribute("NX_MCP_MEASURED_PMI_V1", kind, -1) and (
                    obj.GetStringAttribute("NX_MCP_MEASURED_PMI_V1") != "disabled"
                ):
                    return True
        return False

    def refreshers(self) -> dict:
        return dict(catalogue.hooks("refresher"))

    # -- display state (view rollback, restore_id) ---------------------------------------
    def display_camera(self):
        return self.x._view_info()

    def display_records(self, objects, appearance: bool) -> list:
        return self.x._display_pairs(list(objects), appearance)

    def highlights(self) -> list:
        return list(getattr(self.x, "_highlighted_objects", []))

    def restore_display(self, camera, records, highlights) -> None:
        for obj, record in records:
            self.x._apply_display_record(obj, record)
        if highlights is not None:
            self.x._set_highlights(highlights)
        if camera is not None:
            self.x._restore_camera(camera)

    def save_display_snapshot(self, records, camera) -> str:
        return self.x._save_display_snapshot([r for _, r in records], camera=camera)

    def record_history(self, mark, part, operation_id: str, tool: str) -> None:
        self.x._history.append(
            {
                "mark": mark,
                "part_id": self.part_key(part) if part is not None else None,
                "operation_id": operation_id,
                "method": tool,
            }
        )

    def after_commit(self, spec) -> None:
        if spec.effect != "read":
            self.x._review_epoch = getattr(self.x, "_review_epoch", 0) + 1

    def after_failure(self, spec) -> None:
        self.after_commit(spec)

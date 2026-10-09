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
from nx_mcp.core.references import PREFIX, Counter, References

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
    ("Part", "part"),
)


class LegacyServices:
    """Implements ``nx_mcp.core.tx.Services`` for one ``HardenedExecutor``."""

    def __init__(self, executor) -> None:
        self.x = executor
        if not hasattr(executor, "_owned_parts"):
            executor._owned_parts = set()
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

    def sheet_units(self):
        return None  # Sheet quantities fall back to part units until the drawing domain.

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

    def owns(self, part) -> bool:
        return self.part_key(part) in self.x._owned_parts

    def own(self, part) -> None:
        self.x._owned_parts.add(self.part_key(part))

    def modified_flags(self) -> dict:
        out = {}
        for part in self.parts():
            if not self.owns(part):
                with contextlib.suppress(Exception):
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
        return self.x._snapshot(part)

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
        self.x._active_mark = mark
        self.x._drop_inherited_feature_errors(part, before)
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

    def refresh_annotations(self, spec) -> list:
        refresh = getattr(self.x, "_refresh_annotations", None)
        if refresh is None or self.work_part() is None:
            return []
        return list(refresh().get("updated") or [])

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

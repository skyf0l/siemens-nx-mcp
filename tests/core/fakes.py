"""A small in-memory ``Services`` for kernel tests (no NX).

Objects live in ``FakePart.objects``; an undo mark copies that list, so undo
restores structure exactly like NX undo restores the model.
"""

from __future__ import annotations

import contextlib
import itertools
from pathlib import Path
from types import SimpleNamespace as NS

from nx_mcp.bridge import ObjectRegistry
from nx_mcp.core.receipts import OperationStore
from nx_mcp.core.references import Counter, References
from nx_mcp.core.tx import Services
from nx_mcp.workspace import Workspace

_tags = itertools.count(1000)


class Obj:
    def __init__(self, kind, name="", part=None, jid=None):
        self.Tag = next(_tags)
        self.kind = kind
        self.Name = name
        self.JournalIdentifier = jid or f"{kind.upper()}({self.Tag})"
        self.OwningPart = part
        self.errors: list[str] = []

    def GetFeatureErrorMessages(self):
        return list(self.errors)


class FakePart:
    def __init__(self, path: Path, units="mm"):
        self.Tag = next(_tags)
        self.FullPath = str(path)
        self.Name = path.stem
        self.PartUnits = units
        self.IsModified = False
        self.objects: list[Obj] = []

    def add(self, kind, name="", jid=None):
        obj = Obj(kind, name, self, jid)
        self.objects.append(obj)
        self.IsModified = True
        return obj

    @property
    def Features(self):
        return [o for o in self.objects if o.kind == "feature"]

    @property
    def Bodies(self):
        return [o for o in self.objects if o.kind == "body"]

    @property
    def Sketches(self):
        return [o for o in self.objects if o.kind == "sketch"]


class FakeServices(Services):
    def __init__(self, root: Path, *, units="mm", dev=True, build="v2406", owns_all=True):
        self.root = Path(root)
        # Every part is the agent's; False: only the ``owned`` ones (gate 6 refuses the
        # rest, the engineer's), in development mode too.
        self.owns_all = owns_all
        self.workspace = Workspace(self.root)
        self.store = OperationStore(self.root)
        self.session_id = "s1"
        self.build = build
        self.dev = dev
        self.nx = NS()
        self.uf = None
        self.session = NS(ActiveSketch=None)
        self.part = FakePart(self.root / "a.prt", units)
        self.extra_parts: list[FakePart] = []
        self.registry = ObjectRegistry()
        self.references = References(self, Counter(self.root / ".nx-mcp" / "refs.json"))
        self.marks: dict[int, list] = {}
        self.mark_ids = itertools.count(1)
        self.mark_names: list[str] = []
        self.undone: list[int] = []
        self.history: list[dict] = []
        self.owned: set[str] = set()
        self.application = "modeling"
        self.fail_undo = False
        self.epoch = 0
        self.current = None  # current drawing sheet
        self.camera = {"scale": 1.0}
        self.highlighted: list = []
        self.snapshots: dict = {}
        self.fail_camera = False
        self.fail_restore = False

    # parts
    def work_part(self):
        return self.part

    def parts(self):
        return [p for p in [self.part, *self.extra_parts] if p is not None]

    def part_key(self, part):
        return f"part_{part.Tag}"

    def part_path(self, part):
        return Path(part.FullPath).name

    def owning_part(self, obj):
        return obj if isinstance(obj, FakePart) else obj.OwningPart

    def part_units(self, part):
        return part.PartUnits

    # drawing sheets: Obj("drawing_sheet") with .units, Obj("drawing_view") with .sheet
    def current_sheet(self):
        return self.current

    def sheet_of(self, obj):
        if getattr(obj, "kind", None) == "drawing_sheet":
            return obj
        return getattr(obj, "sheet", None) if getattr(obj, "kind", None) == "drawing_view" else None

    def sheet_units(self, sheet):
        return sheet.units

    def add_sheet(self, units="mm", current=False):
        sheet = self.part.add("drawing_sheet", "SHT")
        sheet.units = units
        if current:
            self.current = sheet
        return sheet

    def add_view(self, sheet):
        view = self.part.add("drawing_view", "FRONT@1")
        view.sheet = sheet
        return view

    # display state: objects carry .blanked/.color; camera and highlights are plain data
    def display_camera(self):
        if self.fail_camera:
            raise RuntimeError("no view")
        return dict(self.camera)

    def display_records(self, objects, appearance):
        out = []
        for obj in objects:
            record = {"blanked": getattr(obj, "blanked", False)}
            if appearance:
                record["color"] = getattr(obj, "color", 7)
            out.append((obj, record))
        return out

    def highlights(self):
        return list(self.highlighted)

    def restore_display(self, camera, records, highlights):
        if self.fail_restore:
            raise RuntimeError("restore refused")
        for obj, record in records:
            obj.blanked = record["blanked"]
            if "color" in record:
                obj.color = record["color"]
        if highlights is not None:
            self.highlighted = list(highlights)
        if camera is not None:
            self.camera = dict(camera)

    def save_display_snapshot(self, records, camera):
        token = f"display_{len(self.snapshots) + 1}"
        self.snapshots[token] = {"records": records, "camera": camera}
        return token

    def object_of_tag(self, tag):
        for part in self.parts():
            if int(part.Tag) == int(tag):
                return part
            for obj in part.objects:
                if int(obj.Tag) == int(tag):
                    return obj
        return None

    def infer_kind(self, obj, default="error"):
        return "part" if isinstance(obj, FakePart) else obj.kind

    # object registry handles
    def register_handle(self, obj, kind, part):
        return self.registry.register(obj, kind=kind, name=obj.Name, part_id=self.part_key(part)).id

    def resolve_handle(self, handle):
        return self.registry.resolve(handle)

    def handle_live(self, handle):
        return handle in self.registry._objects

    def handle_kind(self, handle):
        entry = self.registry._objects.get(handle)
        return entry.reference.kind if entry else None

    def handle_name(self, handle):
        return self.registry.display_name(handle)

    # state
    def state(self):
        return {
            "work_part": self.part is not None,
            "active_sketch": self.session.ActiveSketch is not None,
            "drawing_sheet": False,
            "assembly": False,
            "interactive": True,
            "application": self.application,
        }

    def owns(self, part):
        return self.owns_all or self.part_key(part) in self.owned

    def own(self, part):
        self.owned.add(self.part_key(part))

    def modified_flags(self):
        return {self.part_key(p): p.IsModified for p in self.parts() if not self.owns(p)}

    # undo and snapshots
    @contextlib.contextmanager
    def observe(self):
        yield NS(touched=False)

    def set_mark(self, name, visible=True):
        mark = next(self.mark_ids)
        self.marks[mark] = [(p, list(p.objects), p.IsModified) for p in self.parts()]
        self.mark_names.append(name)
        return mark

    def undo_to(self, mark):
        if self.fail_undo:
            raise RuntimeError("undo refused")
        for part, objects, modified in self.marks[mark]:
            part.objects[:] = objects
            part.IsModified = modified
        self.undone.append(mark)

    def delete_mark(self, mark):
        self.marks.pop(mark, None)

    def mark_exists(self, mark):
        return mark in self.marks

    def snapshot(self, part):
        return {
            (o.kind, o.Tag): {
                "id": self.register_handle(o, o.kind, part),
                "kind": o.kind,
                "name": o.Name,
                "part_id": self.part_key(part),
            }
            for o in part.objects
        }

    def describe(self, entry, live):
        if live and self.handle_live(entry["id"]):
            obj = self.registry._objects[entry["id"]].value
            return self.references.row(obj, obj.kind)
        return {"id": entry["id"], "kind": entry["kind"], "name": entry["name"], "part": None}

    def feature_errors(self, part, before, mark):
        return [
            {"feature": f.Name, "message": "; ".join(f.errors)}
            for f in part.Features
            if ("feature", f.Tag) not in before and f.errors
        ]

    def clear_update_errors(self):
        pass

    def invalidate(self, before, after, topology):
        """Like the host: deleted objects, and for a topology change the faces and
        edges no longer in their part (``part.objects``); returns the retired handles."""
        removed = {v["id"] for k, v in before.items() if k not in after}
        live = {id(o) for p in self.parts() for o in p.objects}
        retired = set()
        for key, entry in list(self.registry._objects.items()):
            dead = entry.reference.kind in ("face", "edge") and id(entry.value) not in live
            if key in removed or (topology and dead):
                self.registry._objects.pop(key)
                self.registry._stale_ids.add(key)
                retired.add(key)
        return retired

    def invalidate_parts(self, parts):
        for part in parts:
            self.registry.invalidate_part(self.part_key(part))

    def retire_undone(self, parts):
        """Handles whose object the undo removed; ``retag`` objects come back under a
        new tag (an NX build that renumbers on undo)."""
        live = {id(o) for p in parts for o in p.objects} | {id(p) for p in parts}
        retired = set()
        for key, entry in list(self.registry._objects.items()):
            obj = entry.value
            if id(obj) not in live or getattr(obj, "retagged", False):
                self.registry._objects.pop(key)
                self.registry._stale_ids.add(key)
                retired.add(key)
        self.registry._identities = {
            k: v for k, v in self.registry._identities.items() if v in self.registry._objects
        }
        return retired

    def record_history(self, mark, part, operation_id, tool):
        if not self.mark_exists(mark):  # an undo went back past its own mark (as the host)
            return
        self.history.append(
            {
                "mark": mark,
                "part_id": self.part_key(part) if part is not None else None,
                "operation_id": operation_id,
                "method": tool,
            }
        )

    def undo_history(self):
        return self.history

    def after_commit(self, spec):
        self.epoch += 1

    def revision(self):
        return self.epoch

    def after_failure(self, spec, outcome=None):
        if outcome != "not_started":
            self.epoch += 1

"""A small in-memory ``Services`` for kernel tests (no NX, no legacy executor).

Objects live in ``FakePart.objects``; an undo mark copies that list, so undo
restores structure exactly like NX undo restores the model.
"""

from __future__ import annotations

import contextlib
import itertools
from pathlib import Path
from types import SimpleNamespace as NS

from nx_mcp.bridge import ObjectRegistry
from nx_mcp.core.references import Counter, References
from nx_mcp.core.tx import Services
from nx_mcp.recovery import OperationStore
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
    def __init__(self, root: Path, *, units="mm", dev=True, build="v2406"):
        self.root = Path(root)
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
        self.refreshed: list[dict] = []
        self.epoch = 0

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

    def sheet_units(self):
        return None

    def infer_kind(self, obj, default="error"):
        return "part" if isinstance(obj, FakePart) else obj.kind

    # legacy registry
    def legacy_register(self, obj, kind, part):
        return self.registry.register(obj, kind=kind, name=obj.Name, part_id=self.part_key(part)).id

    def legacy_resolve(self, legacy_id):
        return self.registry.resolve(legacy_id)

    def legacy_live(self, legacy_id):
        return legacy_id in self.registry._objects

    def legacy_kind(self, legacy_id):
        entry = self.registry._objects.get(legacy_id)
        return entry.reference.kind if entry else None

    def legacy_name(self, legacy_id):
        return self.registry.display_name(legacy_id)

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
        return self.part_key(part) in self.owned

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

    def snapshot(self, part):
        return {
            (o.kind, o.Tag): {
                "id": self.legacy_register(o, o.kind, part),
                "kind": o.kind,
                "name": o.Name,
                "part_id": self.part_key(part),
            }
            for o in part.objects
        }

    def describe(self, entry, live):
        if live and self.legacy_live(entry["id"]):
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
        removed = {v["id"] for k, v in before.items() if k not in after}
        for key, entry in list(self.registry._objects.items()):
            if key in removed or (topology and entry.reference.kind in ("face", "edge")):
                self.registry._objects.pop(key)
                self.registry._stale_ids.add(key)

    def invalidate_parts(self, parts):
        for part in parts:
            self.registry.invalidate_part(self.part_key(part))

    def refresh_annotations(self, spec):
        return list(self.refreshed)

    def record_history(self, mark, part, operation_id, tool):
        self.history.append({"mark": mark, "operation_id": operation_id, "method": tool})

    def after_commit(self, spec):
        self.epoch += 1

    def after_failure(self, spec):
        self.epoch += 1

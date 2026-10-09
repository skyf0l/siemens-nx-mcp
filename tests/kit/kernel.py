"""``run(nx, tool, **args)``: call a catalogue tool on a ``FakeNX`` through the real kernel.

``KitServices`` implements ``nx_mcp.core.tx.Services`` over the fake session:
undo marks are the fake session's marks, references are the fake's ref table
(``nx.refs``), receipts go to a temporary workspace, and development mode is on
(untested tools run, ownership is not enforced). Every kernel gate runs.

Sheet tools get the sheet their arguments name (``nx.sheet(units=...)``,
``nx.view(sheet)``) and ``tx.units["sheet_length"]``. Display state for ``view``
tools is the fake objects' ``IsBlanked``/``Color``/``transparency``/``highlighted``,
``nx.camera`` and ``nx.highlighted``; ``nx.display_snapshots`` holds restore_ids.
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from nx_mcp.core import catalogue, kernel
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Services
from nx_mcp.core.types import expand_kinds
from nx_mcp.kit import drawing, units

KINDS = ("Bodies", "body"), ("Features", "feature"), ("Sketches", "sketch"), ("Curves", "curve")


class _Refs:
    """The ``References`` surface the kernel uses, over ``FakeNX.refs``."""

    def __init__(self, fake):
        self.fake = fake

    def resolve(self, ref, kinds=()):
        entry = self.fake.refs.get(ref)
        if entry is None:
            fail("NX_STALE_REFERENCE", f"{ref} is not a live reference", details={"ref": ref})
        obj, kind = entry
        if kinds and kind not in expand_kinds(kinds):
            fail("NX_WRONG_KIND", f"{ref} is a {kind}", details={"ref": ref, "kind": kind})
        return obj

    def row(self, obj, kind, *, part=None, anchor=False):
        return self.fake.row(obj, kind)

    def name_of(self, ref):
        entry = self.fake.refs.get(ref)
        return getattr(entry[0], "Name", None) if entry else None

    def kind_of(self, obj):
        return next((k for o, k in self.fake.refs.values() if o is obj), None)


class KitServices(Services):
    def __init__(self, fake, root=None, build="v2406"):
        from nx_mcp.recovery import OperationStore
        from nx_mcp.workspace import Workspace

        self.fake = fake
        self.root = Path(root or tempfile.mkdtemp(prefix="nx-kit-"))
        self.workspace = Workspace(self.root)
        self.store = OperationStore(self.root)
        self.build = build
        self.dev = True
        self.session_id = "kit"
        self.references = _Refs(fake)
        self.history: list[dict] = []
        self.owned: set[str] = set()
        self.revisions = 0  # Services.revision(): bumped by every non-read call

    nx = property(lambda self: self.fake.nx)
    uf = property(lambda self: self.fake.uf)
    session = property(lambda self: self.fake.session)

    def work_part(self):
        return self.fake.session.Parts.Work

    def parts(self):
        return list(self.fake.session.Parts)

    def part_key(self, part):
        return f"part_{part.Tag}"

    def part_path(self, part):
        return Path(part.FullPath).name

    def owning_part(self, obj):
        return getattr(obj, "OwningPart", None) or self.work_part()

    def part_units(self, part):
        return "mm" if part.PartUnits == "mm" else "in"

    def current_sheet(self):
        return drawing.current(self.work_part())

    def sheet_of(self, obj):
        kind = self.references.kind_of(obj)
        if kind == "drawing_sheet":
            return obj
        if kind == "drawing_view":
            matches = drawing.sheets_showing(self.work_part(), obj)
            return matches[0] if len(matches) == 1 else None
        for locate in catalogue.hooks("sheet").values():  # annotations, dimensions, tables
            sheet = locate(self, obj)
            if sheet is not None:
                return sheet
        return None

    def sheet_units(self, sheet):
        return units.sheet_units(self, sheet)

    # display state (view rollback, restore_id)
    def display_camera(self):
        return dict(self.fake.camera)

    def display_records(self, objects, appearance):
        if appearance:  # like the host: a body is followed by its faces (unique by tag)
            expanded = [x for o in objects for x in [o, *getattr(o, "faces", [])]]
            objects = list({x.Tag: x for x in expanded}.values())
        out = []
        for obj in objects:
            record = {"blanked": bool(obj.IsBlanked)}
            if appearance:
                record.update(color_index=obj.Color, transparency=obj.transparency)
            out.append((obj, record))
        return out

    def highlights(self):
        return list(self.fake.highlighted)

    def restore_display(self, camera, records, highlights):
        for obj, record in records:
            obj.IsBlanked = record["blanked"]
            if "color_index" in record:
                obj.Color, obj.transparency = record["color_index"], record["transparency"]
        if highlights is not None:
            for obj in self.fake.highlighted:
                obj.Unhighlight()
            for obj in highlights:
                obj.Highlight()
            self.fake.highlighted[:] = highlights
        if camera is not None:
            self.fake.camera = dict(camera)

    def save_display_snapshot(self, records, camera):
        """Like the host: into ``kit.display.snapshots`` (records name their reference);
        ``nx.display_snapshots`` mirrors what was saved."""
        from nx_mcp.kit import display

        rows = [{**record, "object": self.fake.row(obj)} for obj, record in records]
        token = display.snapshots.save(self.part_key(self.work_part()), rows, camera)
        self.fake.display_snapshots[token] = {"records": records, "camera": camera}
        return token

    def infer_kind(self, obj, default="error"):
        return self.references.kind_of(obj) or type(obj).__name__.lower()

    def state(self):
        part = self.work_part()
        sheets = getattr(part, "DrawingSheets", None)
        return {
            "work_part": part is not None,
            "active_sketch": self.fake.session.ActiveSketch is not None,
            "drawing_sheet": getattr(sheets, "CurrentDrawingSheet", None) is not None,
            "assembly": getattr(getattr(part, "ComponentAssembly", None), "RootComponent", None)
            is not None,
            "interactive": True,
            "application": "modeling",
        }

    def owns(self, part):
        return True

    def own(self, part):
        self.owned.add(self.part_key(part))

    def modified_flags(self):
        return {}

    @contextlib.contextmanager
    def observe(self):
        yield NS(touched=False)

    def set_mark(self, name, visible=True):
        return self.fake.session.SetUndoMark(1 if visible else 0, name)

    def undo_to(self, mark):
        self.fake.session.UndoToMark(mark, None)

    def delete_mark(self, mark):
        self.fake.session.DeleteUndoMark(mark, None)

    def snapshot(self, part):
        return {
            (kind, o.Tag): {"obj": o, "kind": kind}
            for name, kind in KINDS
            for o in getattr(part, name, [])
        }

    def describe(self, entry, live):
        return self.fake.row(entry["obj"], entry["kind"])

    def feature_errors(self, part, before, mark):
        return [
            {"feature": f.Name, "message": "; ".join(f.GetFeatureErrorMessages())}
            for f in part.Features
            if ("feature", f.Tag) not in before
            and hasattr(f, "GetFeatureErrorMessages")
            and list(f.GetFeatureErrorMessages())
        ]

    def clear_update_errors(self):
        pass

    def invalidate(self, before, after, topology):
        gone = {id(v["obj"]) for k, v in before.items() if k not in after}
        for ref, (obj, kind) in list(self.fake.refs.items()):
            if id(obj) in gone or (topology and kind in ("face", "edge")):
                del self.fake.refs[ref]

    def invalidate_parts(self, parts):
        self.fake.refs.clear()

    def forget_parts(self, keys):
        """Closed parts: their refs go stale (``nx.forgotten`` records the keys)."""
        keys = set(keys)
        forgotten = self.fake.__dict__.setdefault("forgotten", [])
        forgotten.extend(sorted(keys))
        for ref, (obj, _kind) in list(self.fake.refs.items()):
            owner = getattr(obj, "OwningPart", obj if hasattr(obj, "PartUnits") else None)
            if owner is not None and self.part_key(owner) in keys:
                del self.fake.refs[ref]

    def refresh_annotations(self, spec):
        return []

    def record_history(self, mark, part, operation_id, tool):
        self.history.append({"mark": mark, "operation_id": operation_id, "method": tool})

    def revision(self):
        return self.revisions

    def after_commit(self, spec):
        if spec.effect != "read":
            self.revisions += 1

    def after_failure(self, spec):
        self.after_commit(spec)


class Result:
    """A tool envelope with attribute access (``ok.mutation_outcome``)."""

    def __init__(self, envelope):
        self.envelope = envelope

    def __getattr__(self, name):
        return self.envelope.get(name)

    def __repr__(self):
        return f"Result({self.envelope!r})"


def services(nx) -> KitServices:
    if getattr(nx, "_kit_services", None) is None:
        nx._kit_services = KitServices(nx)
    return nx._kit_services


def run(nx, tool, *, operation_id=None, **args):
    """Run catalogue ``tool`` on ``nx`` through ``core.kernel.run``; returns a Result."""
    spec = catalogue.load().get(tool)
    if spec is None:
        raise KeyError(f"{tool} is not a catalogue tool")
    return Result(kernel.run(spec, args, kernel.Context(services(nx), operation_id=operation_id)))

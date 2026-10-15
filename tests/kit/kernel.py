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
from nx_mcp.core.references import ANCHORED, HANDLE_ANCHOR, article
from nx_mcp.core.tx import Services
from nx_mcp.core.types import expand_kinds
from nx_mcp.kit import drawing, expr, units

KINDS = ("Bodies", "body"), ("Features", "feature"), ("Sketches", "sketch"), ("Curves", "curve")
COLLECTIONS = {"body": "Bodies", "feature": "Features", "sketch": "Sketches"}


class _Refs:
    """The ``References`` surface the kernel uses, over ``FakeNX.refs``.

    Like ``core.references``: features, bodies and sketches whose test set a
    ``JournalIdentifier`` (kit objects default to ``OBJECT(<tag>)``, which gets none),
    and any row asked with ``anchor=True``, carry ``anchor``
    ``@<part file>:<JournalIdentifier>``; :meth:`find_anchor` finds its unique current
    match, and an anchor resolves wherever a ref does."""

    def __init__(self, fake):
        self.fake = fake

    def resolve(self, ref, kinds=()):
        handle = HANDLE_ANCHOR.match(ref)
        if handle is not None:  # nx_create_anchor's body/face/edge anchor, as core resolves it
            return self._handle_anchor(ref, handle, kinds)
        if ref.startswith("@"):
            for kind in sorted(expand_kinds(kinds) & ANCHORED if kinds else ANCHORED):
                row = self.find_anchor(ref, kind)
                if row is not None:
                    return self.fake.refs[row["id"]][0]
            fail("NX_NOT_FOUND", f"{ref} names no current object", details={"ref": ref})
        entry = self.fake.refs.get(ref)
        if entry is None:
            fail("NX_STALE_REFERENCE", f"{ref} is not a live reference", details={"ref": ref})
        obj, kind = entry
        if kinds and kind not in expand_kinds(kinds):
            wanted = sorted(expand_kinds(kinds))
            fail(
                "NX_WRONG_KIND",
                f"{ref} is {article(kind)} {kind}; expected {'/'.join(wanted)}",
                details={"ref": ref, "kind": kind, "expected": wanted},
            )
        return obj

    def _handle_anchor(self, ref, match, kinds):
        kind = match.group("kind")
        if kinds and kind not in expand_kinds(kinds):
            wanted = sorted(expand_kinds(kinds))
            fail("NX_WRONG_KIND", f"{ref} is {article(kind)} {kind}; expected {'/'.join(wanted)}",
                 details={"ref": ref, "kind": kind, "expected": wanted})  # fmt: skip
        found = None
        with contextlib.suppress(Exception):
            tag = int(self.fake.uf.Tag.AskTagOfHandle(match.group("handle")))
            for part in self.fake.session.Parts:
                pool = list(getattr(part, "Curves", []) or []) if kind == "curve" else []
                for body in getattr(part, "Bodies", []) or []:
                    if kind == "body":
                        pool.append(body)
                    elif kind in ("face", "edge"):
                        pool.extend(body.GetFaces() if kind == "face" else body.GetEdges())
                found = found or next((o for o in pool if int(o.Tag) == tag), None)
        if found is None:
            fail("NX_STALE_REFERENCE", f"{ref}: the anchored {kind} no longer exists",
                 details={"ref": ref, "kind": kind})  # fmt: skip
        return found

    def row(self, obj, kind, *, part=None, anchor=False):
        row = self.fake.row(obj, kind)
        jid = str(getattr(obj, "JournalIdentifier", "") or "")
        # Kit objects default to a placeholder OBJECT(<tag>): anchors only for real ones.
        if jid and (anchor or (row["kind"] in ANCHORED and not jid.startswith("OBJECT("))):
            owner = part or getattr(obj, "OwningPart", None) or self.fake.part
            row["anchor"] = f"@{Path(owner.FullPath).name}:{jid}"
        return row

    def find_anchor(self, anchor, kind):
        """The row of the unique object of ``kind`` whose anchor is ``anchor``."""
        name = COLLECTIONS.get(kind)
        if name is None or not anchor.startswith("@") or ":" not in anchor:
            return None
        file, jid = anchor[1:].rsplit(":", 1)
        found = [
            obj
            for part in self.fake.session.Parts
            if Path(part.FullPath).name == file
            for obj in getattr(part, name, [])
            if str(getattr(obj, "JournalIdentifier", "") or "") == jid
        ]
        return self.row(found[0], kind) if len(found) == 1 else None

    def stale(self, retired, floor=None):
        """Kit handles are the refs themselves."""
        return sorted(retired)

    def name_of(self, ref):
        entry = self.fake.refs.get(ref)
        return getattr(entry[0], "Name", None) if entry else None

    def kind_of(self, obj):
        return next((k for o, k in self.fake.refs.values() if o is obj), None)

    def rebind(self, retired, floor=None):
        """Kit objects keep their identity across undo: nothing moves, retired refs
        are stale."""
        return {"rebound": [], "stale": sorted(retired)}


class KitServices(Services):
    def __init__(self, fake, root=None, build="v2406"):
        from nx_mcp.core.receipts import OperationStore
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
        self.checkpoints: dict[str, dict] = {}
        self.owned: set[str] = set()
        self.foreign: set[str] = set()  # part keys of parts the engineer opened
        self.revisions = 0  # Services.revision(): bumped by every non-read call

    nx = property(lambda self: self.fake.nx)
    uf = property(lambda self: self.fake.uf)
    session = property(lambda self: self.fake.session)

    def work_part(self):
        if self.bound is not None:  # gate 4s: a read bound to a part that is not work
            return self.bound
        return self.fake.session.Parts.Work

    def switch(self, part, mode, *, application=None):
        """Gate 4s through the real ``kit.switching`` on the fake session."""
        from nx_mcp.kit import switching

        fake = self.fake
        tx = NS(session=fake.session, nx=fake.nx, uf=fake.uf, workspace=self.workspace)
        return switching.Switch(tx, part, mode, application=application).enter()

    def parts(self):
        return list(self.fake.session.Parts)

    def part_key(self, part):
        """``part_<tag>``, kept by the part: NX's wrapper of a closed part reports tag 0."""
        key = getattr(part, "_kit_part_key", None)
        if key is None:
            key = f"part_{part.Tag}"
            with contextlib.suppress(AttributeError):
                part._kit_part_key = key
        return key

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

    def evaluate_formula(self, formula, dim, unit, *, field=None, scratch=False):
        return expr.evaluate(self, formula, dim, unit, field=field, scratch=scratch)

    # display state (view rollback, restore_id)
    def display_camera(self):
        return dict(self.fake.camera)

    def display_records(self, objects, appearance):
        """Like the host: one record per object given (unique by tag); a body's appearance
        record names only its faces that differ (``display.body_appearance``)."""
        from nx_mcp.kit import display

        out = []
        for obj in {x.Tag: x for x in objects}.values():
            record = {"blanked": bool(obj.IsBlanked)}
            if appearance and type(obj).__name__ == "Body":
                record.update(
                    display.body_appearance(
                        obj,
                        list(getattr(obj, "faces", [])),
                        lambda face: face.transparency,
                        lambda face: self.fake.row(face, "face"),
                    )  # fmt: skip
                )
            elif appearance:
                record["color_index"] = obj.Color
                if type(obj).__name__ == "Face":  # like the host: translucency of faces
                    record["transparency"] = obj.transparency
            out.append((obj, record))
        return out

    def highlights(self):
        return list(self.fake.highlighted)

    def restore_display(self, camera, records, highlights):
        for obj, record in records:
            obj.IsBlanked = record["blanked"]
            if "color_index" in record:
                obj.Color = record["color_index"]
            if "transparency" in record:
                obj.transparency = record["transparency"]
            if type(obj).__name__ == "Body" and "color_index" in record:  # body -> faces
                for face in getattr(obj, "faces", []):
                    face.Color = record["color_index"]
                    if record.get("transparency") is not None:
                        face.transparency = record["transparency"]
            for row in record.get("faces") or ():
                face = self.fake.refs[row["object"]["id"]][0]
                face.Color, face.transparency = row["color_index"], row["transparency"]
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
            "application": {"UG_APP_SBSM": "sheet_metal"}.get(
                str(getattr(self.fake.session, "ApplicationName", "") or ""), "modeling"
            ),
        }

    def release_drafting_sketch(self):
        """Like the host: ``kit.sketch.release_drafting_sketch`` on the fake session."""
        from nx_mcp.kit import sketch

        return sketch.release_drafting_sketch(self.fake.session, self.fake.nx)

    def owns(self, part):
        """Every part is the agent's, except those a test put in ``foreign`` (the
        engineer's own parts: never counted by gate 3t)."""
        return self.part_key(part) not in self.foreign

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
        """Like the host: deleted objects, and for a topology change the faces and
        edges no body of a loaded part has any more; returns the retired refs."""
        gone = {id(v["obj"]) for k, v in before.items() if k not in after}
        live = set()
        if topology:
            for part in self.parts():
                for body in getattr(part, "Bodies", []):
                    for method in ("GetFaces", "GetEdges"):
                        with contextlib.suppress(Exception):
                            live.update(id(x) for x in getattr(body, method)())
        retired = set()
        for ref, (obj, kind) in list(self.fake.refs.items()):
            if id(obj) in gone or (topology and kind in ("face", "edge") and id(obj) not in live):
                del self.fake.refs[ref]
                retired.add(ref)
        return retired

    def invalidate_parts(self, parts):
        self.fake.refs.clear()

    def retire_undone(self, parts):
        """Refs of bodies, features, sketches and curves the undo removed."""
        live = {id(o) for p in parts for name, _ in KINDS for o in getattr(p, name, [])}
        kinds = {kind for _, kind in KINDS}
        retired = set()
        for ref, (obj, kind) in list(self.fake.refs.items()):
            if kind in kinds and id(obj) not in live:
                del self.fake.refs[ref]
                retired.add(ref)
        return retired

    def forget_parts(self, keys):
        """Closed parts: their refs go stale (``nx.forgotten`` records the keys); like the
        host, their undo history and redo items go (barriers stay) and their checkpoints
        stay listed as ``part_closed``."""
        from nx_mcp.core import marks

        keys = set(keys)
        forgotten = self.fake.__dict__.setdefault("forgotten", [])
        forgotten.extend(sorted(keys))
        for ref, (obj, _kind) in list(self.fake.refs.items()):
            owner = getattr(obj, "OwningPart", obj if hasattr(obj, "PartUnits") else None)
            if owner is not None and self.part_key(owner) in keys:
                del self.fake.refs[ref]
        marks.remove_entries(
            self, [h for h in self.history if marks.barrier(h) is None and h.get("part_id") in keys]
        )
        redo = self.redo_steps()
        redo[:] = [h for h in redo if h.get("part_id") not in keys]
        for cp in self.checkpoints.values():
            if cp.get("part_id") in keys and cp.get("expired") in (None, "mark_evicted"):
                cp["expired"] = "part_closed"

    def engineer_changed(self, display_changed=False):
        """What the interactive host does when the engineer changed the model between
        agent calls (``NXServices.engineer_changed``): an engineer barrier."""
        from nx_mcp.core import marks

        marks.engineer_change(self, display_changed)
        self.revisions += 1

    def record_history(self, mark, part, operation_id, tool):
        if not self.mark_exists(mark):  # like the host: an undo went back past its mark
            return
        key = self.part_key(part) if part is not None else None
        self.history.append(
            {"mark": mark, "part_id": key, "operation_id": operation_id, "method": tool}
        )

    def undo_history(self):
        return self.history

    def undo_checkpoints(self):
        return self.checkpoints

    def revision(self):
        return self.revisions

    def after_commit(self, spec):
        if spec.effect != "read":
            self.revisions += 1

    def after_failure(self, spec, outcome=None):
        if outcome not in ("not_started", "not_applicable"):  # like the host
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

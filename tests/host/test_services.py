"""NXServices and host dispatch: the NX side of every bridge call."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError, classify
from nx_mcp.host import dispatch
from nx_mcp.host.services import NXServices
from nx_mcp.nx_bridge import start_bridge, stop_bridge
from nx_mcp.workspace import Workspace
from tests.kit.objects import Body


class Parts(list):
    Work = Display = None


class FakeSession:
    def __init__(self):
        self.Parts = Parts()
        self.values = []
        self.marks = {}
        self.next_mark = 0
        self.IsBatch = False

    def SetUndoMark(self, *args):
        self.next_mark += 1
        self.marks[self.next_mark] = list(self.values)
        return self.next_mark

    def UndoToMark(self, mark, *args):
        self.values[:] = self.marks[mark]

    def DeleteUndoMark(self, mark, *args):
        self.marks.pop(mark, None)

    def DoesUndoMarkExist(self, mark, *args):
        return mark in self.marks


NXOPEN = NS(
    Session=NS(MarkVisibility=NS(Visible=1, Invisible=0, AnyVisibility=2)),
    DisplayPartOption=NS(AllowAdditional=1, ReplaceExisting=0),
    PartDisplayPartWorkPartOption=NS(SameAsDisplay=0),
)


def services(tmp_path, name="ws"):
    return NXServices(FakeSession(), NXOPEN, "test", Workspace(tmp_path / name))


def test_development_tool_failure_rolls_back_its_undo_mark(tmp_path):
    s = services(tmp_path)

    def mutate(fail=False):
        s.session.values.append(1)
        if fail:
            raise RuntimeError("failure after a model change")
        return {"values": list(s.session.values)}

    s.dev_tools["nx_dev_python"] = mutate
    done = dispatch.execute(s, "nx_dev_python", {"operation_id": "op_dev_00001"})
    assert done["status"] == "success" and done["values"] == [1]
    assert [h["operation_id"] for h in s.history] == ["op_dev_00001"]
    with pytest.raises(NXError) as error:
        dispatch.execute(s, "nx_dev_python", {"fail": True})
    assert error.value.details["mutation_outcome"] == "rolled_back"
    assert s.session.values == [1] and s.epoch == 2
    with pytest.raises(NXError) as error:
        dispatch.execute(s, "nx_dev_python", {"unexpected": 1})
    assert error.value.code == "NX_INVALID_ARGUMENT"


class MarkingSession(FakeSession):
    """NX's newest undo mark: a snippet's own SetUndoMark (or NX's) makes it newer."""

    def GetNewestUndoMark(self, visibility):
        assert visibility == 2  # AnyVisibility
        return max(self.marks, default=None)


def test_a_development_snippet_that_changed_nothing_leaves_no_mark_or_operation(tmp_path):
    s = NXServices(MarkingSession(), NXOPEN, "test", Workspace(tmp_path / "ws"))
    s.dev_tools["nx_dev_python"] = lambda: {"result": "42"}
    probe = dispatch.execute(s, "nx_dev_python", {"operation_id": "op_dev_probe1"})
    assert probe["mutation_outcome"] == "not_applicable" and probe["result"] == "42"
    assert s.history == [] and s.session.marks == {} and s.epoch == 0

    def journal():  # a snippet that changes the model under its own mark, as journals do
        s.session.SetUndoMark(1, "edit")
        s.session.values.append(1)

    s.dev_tools["nx_dev_python"] = journal
    done = dispatch.execute(s, "nx_dev_python", {"operation_id": "op_dev_edit01"})
    assert done["mutation_outcome"] == "committed" and s.epoch == 1
    assert [(h["operation_id"], h["method"]) for h in s.history] == [
        ("op_dev_edit01", "nx_dev_python")
    ]
    assert len(s.session.marks) == 2  # the call's own mark and the snippet's


def test_unknown_methods_are_refused_without_dev_tools(tmp_path):
    with pytest.raises(NXError) as error:
        dispatch.execute(services(tmp_path), "nx_dev_python", {"code": "1"})
    assert error.value.code == "NX_UNKNOWN_TOOL"


def test_reference_namespace_rejects_previous_session(tmp_path):
    first = services(tmp_path)
    ref = first.objects.register(NS(Tag=123, Name="Body"), kind="body", name="Body", part_id="p")
    assert ref.id.startswith("obj_" + first.session_id + "_")
    with pytest.raises(NXError) as error:
        services(tmp_path, "other").objects.resolve(ref.id)
    assert error.value.code == "NX_STALE_REFERENCE"


def test_new_feature_inheriting_a_rolled_back_error_is_updated_once(tmp_path):
    # NX 2406 attaches a rolled-back feature's error to the next feature created
    # under the same name; one update of that new feature drops the stale message.
    class Feature:
        def __init__(self, tag, errors):
            self.Tag, self.errors, self.Name = tag, errors, f"F({tag})"

        def GetFeatureErrorMessages(self):
            return self.errors

    s = services(tmp_path)
    old, clean, inherited = Feature(1, ["old error"]), Feature(2, []), Feature(3, ["stale"])
    logged, marks = [], []

    def do_update(mark):
        marks.append(mark)
        for feature in logged:
            feature.errors = []

    s.session.UpdateManager = NS(LogForUpdate=logged.append, DoUpdate=do_update)
    s.session.ActiveSketch = None
    part = NS(Features=[old, clean, inherited])
    assert s.feature_errors(part, {("feature", 1): {}}, 7) == []
    assert logged == [inherited] and marks == [7]
    assert old.errors == ["old error"] and inherited.errors == []


def test_engineer_change_ends_the_undo_history_with_a_barrier_and_bumps_the_revision(tmp_path):
    s = services(tmp_path)
    s.history.append({"mark": 1})
    s.checkpoints["cp"] = {"mark": 1}
    s.redo_steps().append({"operation_id": "op_undone", "part_id": "part_1"})
    s.engineer_changed()
    assert s.history[0] == {"mark": 1} and s.history[1]["engineer"] is True
    assert s.revision() == 1 and s.part_revision("part_1") == 1 and not s.redo_steps()
    assert s.checkpoints["cp"]["expired"] == "engineer_change"  # listed, not dropped
    s.engineer_changed()  # a second edit before the next agent operation: one barrier
    assert len(s.history) == 2


class _Tagged:
    def __init__(self, tag, name=""):
        self.Tag, self.Name, self.JournalIdentifier = tag, name, f"FACE {tag}"


def _block(tmp_path):
    """Services over one part with a body of three faces and two edges."""
    s = services(tmp_path)
    faces = [_Tagged(t) for t in (11, 12, 13)]
    edges = [_Tagged(t) for t in (21, 22)]
    body = NS(Tag=5, GetFaces=lambda: list(faces), GetEdges=lambda: list(edges))
    part = NS(Tag=1, FullPath=str(tmp_path / "ws" / "a.prt"), Name="a", Bodies=[body])
    s.session.Parts.append(part)
    s.session.Parts.Work = part
    return s, part, faces, edges


def test_a_topology_change_retires_only_consumed_faces_and_edges(tmp_path):
    s, part, faces, edges = _block(tmp_path)
    refs = s.references
    rows = [refs.row(o, "face", part=part)["id"] for o in faces]
    erows = [refs.row(o, "edge", part=part)["id"] for o in edges]
    part_ref = refs.row(part, "part", part=part)["id"]
    consumed, deleted = faces.pop(0), edges[0]
    deleted.Tag = 0  # NX dropped it: its wrapper reports tag 0
    retired = s.invalidate({}, {}, True)
    assert refs.stale(retired) == [rows[0], erows[0]]
    assert consumed.Tag == 11  # a consumed face's wrapper may keep its tag
    assert refs.resolve(rows[1], ("face",)) is faces[0]  # untouched, possibly reshaped
    assert refs.resolve(erows[1], ("edge",)) is edges[1]
    assert refs.resolve(part_ref, ("part",)) is part
    with pytest.raises(NXError) as error:
        refs.resolve(rows[0], ("face",))
    assert error.value.code == "NX_STALE_REFERENCE" and "nx_find_geometry" in error.value.hint


def test_revalidation_after_an_outside_edit_keeps_refs_of_surviving_objects(tmp_path):
    s, part, faces, _edges = _block(tmp_path)
    refs = s.references
    keep = refs.row(faces[1], "face", part=part)["id"]
    gone = refs.row(faces[0], "face", part=part)["id"]
    part_ref = refs.row(part, "part", part=part)["id"]
    faces.pop(0)
    summary = s.revalidate()
    assert summary["stale"] == [gone]
    assert refs.resolve(keep, ("face",)) is faces[0]
    assert refs.resolve(part_ref, ("part",)) is part


def test_deleted_rows_without_a_short_ref_name_the_part_by_its_ref(tmp_path):
    s, part, _faces, _edges = _block(tmp_path)
    part_ref = s.references.row(part, "part", part=part)["id"]
    entry = s.register(_Tagged(99, "ENTITY 3 1 1"), "curve", part)
    s.invalidate({("curve", 99): entry}, {}, False)
    row = s.describe(entry, live=False)
    assert row == {"kind": "curve", "name": "ENTITY 3 1 1", "part": part_ref, "gone": True}


def test_a_refused_call_moves_no_revision(tmp_path):
    s = services(tmp_path)
    spec = NS(effect="model")
    s.after_failure(spec, "not_started")
    assert s.revision() == 0
    s.after_failure(spec, "rolled_back")
    assert s.revision() == 1


def test_display_records_name_registry_objects_and_restore_them(rig):
    body = Body()
    rig.part.Bodies.append(body)
    [(obj, record)] = rig.e.display_records([body], False)
    assert obj is body and record["object"]["id"].startswith("obj_")
    assert record["blanked"] is False
    body.Blank()
    rig.e.restore_display(None, [(body, record)], None)
    assert not body.IsBlanked
    restore_id = rig.e.save_display_snapshot([(body, record)], None)
    assert restore_id.startswith("display_")


def test_native_license_failures_get_a_stable_code():
    class NXException(Exception):
        ErrorCode = 948801

    error = classify(NXException("License Error"))
    assert error.code == "NX_LICENSE_UNAVAILABLE" and error.nx_code == 948801


def test_batch_bridge_requires_explicit_feasibility_gate(tmp_path, monkeypatch):
    stop_bridge()
    monkeypatch.delenv("NX_MCP_ALLOW_UNVERIFIED_PYTHON_BRIDGE", raising=False)
    with pytest.raises(RuntimeError, match="has not been verified"):
        start_bridge(tmp_path)


def test_annotations_include_drafting_gdt_and_datum_targets():
    # changes.created comes from this list: drafting GD&T was missing from it.
    from nx_mcp.host.services import part_annotations

    def obj(tag):
        return NS(Tag=tag)

    part = NS(
        Notes=[obj(1)],
        Labels=[],
        Gdts=[obj(2), obj(3)],
        Annotations=NS(DraftingDatumTargets=[obj(4)], Fcfs=[obj(2)]),
    )
    assert sorted(int(o.Tag) for o in part_annotations(part)) == [1, 2, 3, 4]


def test_each_drawing_table_is_one_object():
    # Live NX 2406: a tabular note is a TableSection plus a cells Table, a parts list or
    # hole table also has a TableSection, a title block a cells Table; changes.created
    # listed each table twice.
    from nx_mcp.host.services import _table_tags, part_annotations

    def obj(tag):
        return NS(Tag=tag)

    notes = {10: 11, 20: 21, 30: 31}  # section -> its tabular note (cells table)
    uf = NS(Tabnot=NS(AskTabularNoteOfSection=lambda tag: notes[int(tag)]))
    part = NS(
        Notes=[],
        Labels=[],
        Annotations=NS(
            TableSections=[obj(10), obj(20), obj(30)],
            Tables=[obj(11), obj(21), obj(31), obj(41)],
            PartsLists=[obj(21)],
            HoleTables=[obj(31)],
        ),
        DraftingManager=NS(TitleBlocks=[obj(40)]),
    )
    tables, inner = _table_tags(part, uf)
    assert tables == {10, 21, 31, 40} and inner == {20, 30}
    assert sorted(int(o.Tag) for o in part_annotations(part)) == [10, 20, 21, 30, 31, 40]


class Shown(Parts):
    """Parts that change the display (and with it the work) part like NX; the host
    restores them only with the variant that keeps NX's undo marks."""

    def SetDisplay(self, part, *_):
        type(self).Display = type(self).Work = part
        return part, None

    def SetActiveDisplayRetainUndoMarks(self, part, *_):
        type(self).retained = [*getattr(type(self), "retained", []), part]
        return self.SetDisplay(part)

    def SetWork(self, part):
        type(self).Work = part


def test_failed_development_snippet_restores_display_and_work_parts(tmp_path):
    s = services(tmp_path)
    s.session.Parts = shown = Shown()
    assembly, piece, other = NS(Tag=1), NS(Tag=2), NS(Tag=3)
    s.snapshot = lambda part: {}
    Shown.Display, Shown.Work = assembly, piece

    def switch_then_raise():
        shown.SetDisplay(other)
        s.session.values.append(1)
        raise RuntimeError("after a display change")

    s.dev_tools["nx_dev_python"] = switch_then_raise
    with pytest.raises(NXError) as error:
        dispatch.execute(s, "nx_dev_python", {})
    assert error.value.details["mutation_outcome"] == "rolled_back"
    assert s.session.values == [] and Shown.Display is assembly and Shown.Work is piece
    assert Shown.retained == [assembly]  # the display switch kept NX's undo marks


def test_failed_rollback_of_a_development_snippet_is_partial_and_keeps_the_mark(tmp_path):
    s = services(tmp_path)

    def broken_undo(mark, *_):
        raise RuntimeError("undo refused")

    s.session.UndoToMark = broken_undo
    s.dev_tools["nx_dev_python"] = lambda: 1 / 0
    with pytest.raises(NXError) as error:
        dispatch.execute(s, "nx_dev_python", {})
    details = error.value.details
    assert details["mutation_outcome"] == "partial"
    assert details["rollback_errors"] == ["undo: undo refused"]
    assert s.session.marks  # kept for a manual undo


def test_an_undo_revives_refs_of_objects_it_brought_back(tmp_path):
    s, part, faces, _edges = _block(tmp_path)
    refs = s.references
    consumed = faces[0]
    ref = refs.row(consumed, "face", part=part)["id"]
    faces.pop(0)  # a blend consumed it
    assert refs.stale(s.invalidate({}, {}, True)) == [ref]
    faces.insert(0, consumed)  # the undo restored the same face (same tag)
    s.revalidate()  # an outside edit revives nothing
    with pytest.raises(NXError):
        refs.resolve(ref, ("face",))
    s.retire_undone(s.parts())
    assert refs.resolve(ref, ("face",)) is consumed


def test_release_drafting_sketch_deactivates_only_a_drafting_sketch(tmp_path):
    s = services(tmp_path)
    s.nx = NS(
        **vars(NXOPEN), Sketch=NS(ViewReorient=NS(FalseValue=False), UpdateLevel=NS(Model="m"))
    )
    calls = []

    def deactivate(*args):
        calls.append(args)
        s.session.ActiveSketch = None

    s.session.ActiveSketch = None
    assert s.release_drafting_sketch() is False
    model = NS(IsDraftingSketch=False, Deactivate=deactivate)
    s.session.ActiveSketch = model
    assert s.release_drafting_sketch() is False and s.session.ActiveSketch is model
    s.session.ActiveSketch = NS(IsDraftingSketch=True, Deactivate=deactivate)
    assert s.release_drafting_sketch() is True
    assert s.session.ActiveSketch is None and calls == [(False, "m")]


class _Counted:
    """An NX object counting reads of its names (each is an NX call on the host)."""

    reads = 0

    def __init__(self, tag, name):
        self.Tag, self._name = tag, name

    @property
    def Name(self):
        type(self).reads += 1
        return self._name

    @property
    def JournalIdentifier(self):
        type(self).reads += 1
        return self._name.upper()


def _model_part(tmp_path, bodies, features):
    part = NS(
        Tag=1, FullPath=str(tmp_path / "ws" / "m.prt"), Name="m",
        ComponentAssembly=NS(RootComponent=None), Bodies=bodies, Features=features,
        Curves=[], Sketches=[],
    )  # fmt: skip
    return part


def test_a_snapshot_reads_the_names_of_new_objects_only(tmp_path):
    s = services(tmp_path)
    bodies = [_Counted(t, f"body{t}") for t in (5, 6)]
    features = [_Counted(t, f"feat{t}") for t in (7, 8, 9)]
    part = _model_part(tmp_path, bodies, features)
    s.session.Parts.append(part)
    _Counted.reads = 0
    first = s.snapshot(part)
    assert _Counted.reads == 10 and set(first) == {("body", 5), ("body", 6), *(
        ("feature", t) for t in (7, 8, 9))}  # fmt: skip
    _Counted.reads = 0
    features.append(_Counted(10, "feat10"))
    bodies.pop(0)
    second = s.snapshot(part)
    assert _Counted.reads == 2  # only the new feature's names
    assert second[("feature", 7)] == first[("feature", 7)]
    assert second[("feature", 10)]["name"] == "feat10" and ("body", 5) not in second
    row = second[("feature", 10)]
    assert row["journal_id"] == "FEAT10" and row["owner_part_path"] == part.FullPath
    assert s.resolve_handle(row["id"]) is features[-1]


def test_faces_and_edges_live_while_their_body_lists_them(tmp_path):
    s = services(tmp_path)
    reads = []

    class Body:
        def __init__(self, tag, faces):
            self.Tag, self.faces = tag, faces

        def GetFaces(self):
            reads.append(self.Tag)
            return list(self.faces)

        def GetEdges(self):
            return []

    face = _Tagged(31)
    gone = _Tagged(32)
    held, other = Body(5, [face]), Body(6, [_Tagged(t) for t in range(40, 60)])
    face.GetBody = gone.GetBody = lambda: held
    part = _model_part(tmp_path, [held, other], [])
    s.session.Parts.append(part)
    refs = s.references
    keep, lost = (refs.row(f, "face", part=part)["id"] for f in (face, gone))
    retired = s.invalidate({}, {}, True)
    assert refs.stale(retired) == [lost] and refs.resolve(keep, ("face",)) is face
    assert reads == [5]  # the other body's faces were never read

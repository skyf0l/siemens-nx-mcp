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


NXOPEN = NS(Session=NS(MarkVisibility=NS(Visible=1, Invisible=0)))


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


def test_engineer_change_clears_undo_history_and_bumps_the_revision(tmp_path):
    s = services(tmp_path)
    s.history.append({"mark": 1})
    s.checkpoints["cp"] = {"mark": 1}
    s.engineer_changed()
    assert not s.history and not s.checkpoints and s.revision() == 1


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


class Shown(Parts):
    """Parts that change the display (and with it the work) part like NX."""

    def SetDisplay(self, part, *_):
        type(self).Display = type(self).Work = part
        return part, None

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

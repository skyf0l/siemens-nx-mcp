"""host.dispatch.execute: catalogue tools run in the kernel on the host's NXServices;
development tools run under one undo mark."""

from pathlib import Path

import pytest

from nx_mcp.core import catalogue
from nx_mcp.host import dispatch
from nx_mcp.host.dispatch import KernelError
from tests.kit.objects import Part


def test_catalogue_tool_returns_the_envelope(rig):
    body = rig.nx.Body("B1")
    rig.part.Bodies.append(body)
    ok = rig.run("nx_list_objects", {"kind": "body", "intent": "look", "detail": "full"})
    assert ok["ok"] is True and ok["tool"] == "nx_list_objects"
    assert ok["mutation_outcome"] == "not_applicable" and ok["page"]["total"] == 1
    row = ok["data"]["items"][0]
    assert row["id"].startswith("b:") and row["part"].startswith("p:") and row["kind"] == "body"
    assert row["anchor"].startswith("@test.prt:")
    # The short ref and the object registry handle (obj_...) resolve to the same body.
    handle = rig.ref(body)
    by_handle = rig.run("nx_list_objects", {"kind": "body", "owner": handle})
    by_short = rig.run("nx_list_objects", {"kind": "body", "owner": row["id"]})
    assert by_handle["data"]["items"][0]["id"] == by_short["data"]["items"][0]["id"] == row["id"]


def test_catalogue_failure_raises_kernel_error_with_envelope(rig):
    with pytest.raises(KernelError) as error:
        rig.run("nx_list_objects", {"kind": "widget", "operation_id": "op_abcdefgh"})
    payload = error.value.as_dict()
    assert payload["status"] == "error" and payload["code"] == "NX_INVALID_ARGUMENT"
    envelope = payload["details"]["envelope"]
    assert envelope["ok"] is False and envelope["error"]["field"] == "/kind"
    assert envelope["operation_id"] == "op_abcdefgh"
    assert payload["details"]["mutation_outcome"] == "not_applicable"


def test_untested_model_tool_is_refused_in_production(rig, monkeypatch):
    monkeypatch.delenv("NX_MCP_DEV", raising=False)
    with pytest.raises(KernelError) as error:
        rig.run(
            "nx_create_datum_plane",
            {"method": "point_normal", "origin": [0, 0, 0], "normal": [0, 0, 1]},
        )
    assert error.value.code == "NX_VERSION_UNSUPPORTED"
    assert error.value.details["mutation_outcome"] == "not_started"
    assert rig.session.marks == {}


def test_model_tool_in_dev_refused_before_any_mark_or_history(rig, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    with pytest.raises(KernelError) as error:
        rig.run(
            "nx_create_datum_plane",
            {"method": "three_points", "points": [[0, 0, 0], [1, 1, 1], [2, 2, 2]]},
        )
    assert error.value.details["envelope"]["error"]["field"] == "/points"
    assert error.value.details["mutation_outcome"] == "not_started"
    assert rig.session.marks == {} and rig.e.history == []


def test_development_tools_keep_their_shape_and_record_ownership(rig, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")  # tracking runs in development too
    rig.e.dev_tools["nx_test_read"] = lambda: {"objects": []}
    done = rig.run("nx_test_read", {})
    assert done["status"] == "success" and "ok" not in done
    services = rig.e
    rig.e.owned_parts.clear()
    # The engineer's part was loaded before any agent call: not owned.
    assert not services.owns(rig.part)
    # A part loaded by an agent call (created or opened) is owned.
    rig.e.dev_tools["nx_test_open"] = lambda: (
        bool(Part(rig.session, Path(rig.part.FullPath).parent / "new.prt")) and {"opened": 1}
    )
    rig.run("nx_test_open", {})
    new = rig.session.Parts[-1]
    assert services.owns(new) and services.created(new) and not services.owns(rig.part)


def test_a_development_snippets_system_exit_is_a_rolled_back_error(rig):
    from nx_mcp.core.errors import NXError

    def snippet():
        raise SystemExit(2)

    rig.e.dev_tools["nx_test_exit"] = snippet
    with pytest.raises(NXError) as error:
        rig.run("nx_test_exit", {})
    assert error.value.details["mutation_outcome"] == "rolled_back"


def test_panel_summary_comes_from_the_spec(rig):
    line = dispatch.render_summary(
        rig.e, "nx_create_datum_plane", {"method": "offset", "reference": "fc:7", "distance": 15}
    )
    assert line == "Datum plane 15 mm from fc:7"
    assert dispatch.render_summary(rig.e, "nx_test_read", {}) is None
    assert dispatch.is_read_only("nx_list_objects") is True
    assert dispatch.is_read_only("nx_create_datum_plane") is False
    assert dispatch.is_read_only("nx_test_read") is False


def test_snapshot_reports_drawing_tables_with_their_own_kind(rig):
    from types import SimpleNamespace as NS

    from tests.kit.objects import Object

    note, table = Object("NOTE1"), Object("PARTS_LIST1")
    rig.part.Notes.append(note)
    rig.part.Annotations = NS(PartsLists=[table], BendTables=[])
    services = rig.e
    snap = services.snapshot(rig.part)
    assert snap[("annotation", int(table.Tag))]["kind"] == "drawing_table"
    assert snap[("annotation", int(note.Tag))]["kind"] == "annotation"
    row = services.describe(snap[("annotation", int(table.Tag))], live=True)
    assert row["kind"] == "drawing_table" and row["id"].startswith("t:")


def test_gate_13_uses_every_registered_refresher(rig):
    catalogue.load()
    services = rig.e
    assert services.refreshers() == dict(catalogue.hooks("refresher"))
    assert {"drawing_view", "drawing_table", "annotation"} <= set(services.refreshers())


def test_inherited_update_error_is_dropped_with_the_new_sketch_deactivated(rig):
    """NX 2406: after a rolled-back feature, the next sketch's internal datum carries the
    old update error; NX updates it only while no sketch is active (gate 11)."""
    from types import SimpleNamespace as NS

    class Stale:
        Tag, Name, JournalIdentifier = 9001, "SKETCH(3:1B)", "SKETCH(3:1B)"
        messages = ["Unable to terminate the blend faces"]

        def GetFeatureErrorMessages(self):  # noqa: N802 - NXOpen name
            return list(self.messages)

    stale = Stale()
    calls = []

    class Sketch:
        def Deactivate(self, reorient, level):  # noqa: N802
            calls.append(("deactivate", level))
            stale.messages = []  # the model update drops the inherited message

        def Activate(self, reorient):  # noqa: N802
            calls.append(("activate", reorient))

    rig.e.nx.Sketch = NS(ViewReorient=NS(FalseValue=False), UpdateLevel=NS(Model="model"))
    rig.part.Features.append(stale)
    rig.session.ActiveSketch = Sketch()
    services = rig.e
    assert services.feature_errors(rig.part, {}, mark=None) == []
    assert calls == [("deactivate", "model"), ("activate", False)]

    stale.messages = ["a real error"]  # survives the update: still reported

    def keep(reorient, level):
        calls.append(("deactivate", level))

    rig.session.ActiveSketch.Deactivate = keep
    failed = services.feature_errors(rig.part, {}, mark=None)
    assert failed and failed[0]["message"] == "a real error" and calls[-1][0] == "activate"

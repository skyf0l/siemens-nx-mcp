"""nx_preview_changes / nx_finish_preview on the fake kit: report, roll back, accept, stale."""

import pytest

from nx_mcp.core import catalogue
from nx_mcp.core.summary import render
from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body

BOX = [0, 0, 0, 40, 20, 10]


def extrude_nx():
    nx = modeling_nx()
    sk = nx.sketch()
    nx.on_commit("ExtrudeBuilder", lambda b: feature(nx, "EXTRUDE", [new_body(nx, BOX)]))
    return nx, sk


def extrude(sketch):
    return {
        "tool": "nx_create_extrude",
        "arguments": {"sketch": sketch, "end": {"type": "distance", "value": 10}},
    }


def test_preview_reports_the_change_and_leaves_the_model_unchanged():
    nx, sk = extrude_nx()
    marks = len(nx.undo_marks)
    ok = run(nx, "nx_preview_changes", operations=[extrude(sk.ref)])
    assert ok.ok, ok
    assert ok.mutation_outcome == "previewed"
    data = ok.data
    assert data["preview_id"].startswith("preview_") and data["count"] == 1
    assert data["before"]["volume"] is None and data["before"]["counts"]["bodies"] == 0
    assert data["after"]["volume"] == pytest.approx(8000)
    assert data["after"]["bounds"]["size"] == [40, 20, 10]
    assert data["delta"]["volume"] == pytest.approx(8000) and data["delta"]["bodies"] == 1
    assert data["after"]["health"]["healthy"] is True
    assert data["results"][0]["tool"] == "nx_create_extrude"
    assert len(data["results"][0]["changes"]["created"]) >= 1
    assert data["image"] is None
    assert list(nx.part.Bodies) == [] and len(nx.part.Features) == 0
    assert len(nx.undo_marks) == marks  # the preview's mark is gone
    # The rollback invalidates the part's refs: the input sketch gets a new ref.
    fresh = data["references"][sk.ref]
    assert fresh != sk.ref and nx.refs[fresh][0] is sk


def test_accept_reapplies_exactly_the_preview_under_one_mark():
    nx, sk = extrude_nx()
    preview = run(nx, "nx_preview_changes", operations=[extrude(sk.ref)])
    marks = len(nx.undo_marks)
    ok = run(nx, "nx_finish_preview", preview_id=preview.data["preview_id"], action="accept")
    assert ok.ok, ok
    assert ok.mutation_outcome == "committed" and ok.data["count"] == 1
    assert ok.data["results"][0]["data"]["volume"] == pytest.approx(8000)
    assert len(nx.part.Bodies) == 1 and len(nx.undo_marks) == marks + 1
    again = run(nx, "nx_finish_preview", preview_id=preview.data["preview_id"], action="accept")
    assert again.error["code"] == "NX_STALE_PREVIEW" and again.mutation_outcome == "not_started"
    assert len(nx.part.Bodies) == 1


def test_discard_drops_the_preview_and_changes_nothing():
    nx, sk = extrude_nx()
    preview = run(nx, "nx_preview_changes", operations=[extrude(sk.ref)])
    pid = preview.data["preview_id"]
    ok = run(nx, "nx_finish_preview", preview_id=pid, action="discard")
    assert ok.ok and ok.data["action"] == "discard" and ok.data["count"] == 0
    assert list(nx.part.Bodies) == []
    gone = run(nx, "nx_finish_preview", preview_id=pid, action="accept")
    assert gone.error["code"] == "NX_STALE_PREVIEW" and gone.error["category"] == "stale_reference"


def test_an_intervening_change_makes_the_preview_stale():
    nx, sk = extrude_nx()
    preview = run(nx, "nx_preview_changes", operations=[extrude(sk.ref)])
    assert run(nx, "nx_set_expression", name="wall", formula="2", units="mm").ok
    stale = run(nx, "nx_finish_preview", preview_id=preview.data["preview_id"], action="accept")
    assert stale.error["code"] == "NX_STALE_PREVIEW" and stale.mutation_outcome == "not_started"
    assert list(nx.part.Bodies) == []
    # A read in between does not.
    nx2, sk2 = extrude_nx()
    p2 = run(nx2, "nx_preview_changes", operations=[extrude(sk2.ref)])
    assert run(nx2, "nx_measure_bounds", bodies=None).error is not None  # no bodies: a read
    assert run(nx2, "nx_finish_preview", preview_id=p2.data["preview_id"], action="accept").ok


def test_non_previewable_or_failing_steps_are_refused_or_rolled_back():
    nx, sk = extrude_nx()
    body = nx.block(1, 1, 1)
    refused = run(
        nx, "nx_preview_changes",
        operations=[extrude(sk.ref), {"tool": "nx_delete_objects", "arguments": {"objects": [body.ref]}}],
    )  # fmt: skip
    assert refused.error["code"] == "NX_UNSUPPORTED_OPTION"
    assert refused.error["field"] == "/operations/1/tool"
    assert refused.mutation_outcome == "not_started"
    nx.refuse("ExtrudeBuilder")
    failed = run(nx, "nx_preview_changes", operations=[extrude(sk.ref)])
    assert not failed.ok and failed.mutation_outcome == "rolled_back"
    assert failed.error["details"]["child_index"] == 0
    assert list(nx.part.Bodies) == [body] and nx.undo_marks == []


def test_a_placeholder_names_an_earlier_step():
    nx, _sk = extrude_nx()
    sketch = {"tool": "nx_create_sketch", "arguments": {"placement": {"principal": "xy"}}}
    bad = run(nx, "nx_preview_changes", operations=[extrude("$1.data.sketch"), sketch])
    assert bad.error["field"] == "/operations/0/arguments/sketch"
    assert bad.mutation_outcome == "not_started"


def test_summaries():
    spec = catalogue.load()["nx_preview_changes"]
    assert render(spec, {"operations": [extrude("s:1")]}) == "Preview: Extrude sketch"
    finish = catalogue.load()["nx_finish_preview"]
    assert render(finish, {"preview_id": "x", "action": "discard"}) == "Discard the preview"

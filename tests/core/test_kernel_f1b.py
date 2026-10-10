"""Kernel features for domain migrations: sheet units, view rollback, ownership, budget."""

import json

import pytest

from nx_mcp.core import evidence, kernel
from nx_mcp.core import tx as tx_module
from tests.core import tools
from tests.core.fakes import FakePart, FakeServices

SPECS = tools.specs()


@pytest.fixture
def svc(tmp_path):
    return FakeServices(tmp_path)


def run(svc, tool, operation_id=None, **args):
    return kernel.run(SPECS[tool], args, kernel.Context(svc, operation_id=operation_id))


def ref(svc, obj, kind):
    return svc.references.row(obj, kind)["id"]


# -- 1. sheet units -----------------------------------------------------------------


def test_sheet_quantities_use_the_named_sheets_units(svc):
    inch = svc.add_sheet("in")
    ok = run(
        svc,
        "nx_create_label",
        origin=[4, 3],
        height="12.7 mm",
        sheet=ref(svc, inch, "drawing_sheet"),
    )
    assert ok["ok"], ok
    assert ok["data"]["height"] == pytest.approx(0.5)  # converted to the sheet's inches
    assert ok["data"]["origin"] == [4.0, 3.0]  # SheetPoint: plain numbers in sheet units
    assert ok["data"]["units"] == {"length": "mm", "angle": "deg", "sheet_length": "in"}
    assert ok["units"]["sheet_length"] == "in" and ok["units"]["length"] == "mm"
    plain = run(
        svc, "nx_create_label", origin=[1, 1], height=0.25, sheet=ref(svc, inch, "drawing_sheet")
    )
    assert plain["data"]["height"] == 0.25  # a plain number is already in sheet units


def test_a_view_names_its_sheet_and_the_current_sheet_is_the_fallback(tmp_path):
    svc = FakeServices(tmp_path, units="in")
    mm_sheet = svc.add_sheet("mm", current=True)
    inch_sheet = svc.add_sheet("in")
    view = svc.add_view(inch_sheet)
    by_view = run(
        svc, "nx_create_label", origin=[0, 0], height="1 in", view=ref(svc, view, "drawing_view")
    )
    assert by_view["data"]["height"] == 1.0 and by_view["data"]["units"]["sheet_length"] == "in"
    current = run(svc, "nx_create_label", origin=[0, 0], height="1 in")
    assert current["data"]["height"] == pytest.approx(25.4)  # the current mm sheet
    assert current["data"]["sheet"] == mm_sheet.Name
    svc.current = None  # no sheet at all: part units (inches here)
    none = run(svc, "nx_create_label", origin=[0, 0], height="25.4 mm")
    assert none["data"]["height"] == pytest.approx(1.0)
    assert none["data"]["units"]["sheet_length"] == "in" and none["data"]["sheet"] is None


def test_a_new_sheet_uses_its_units_argument_and_stale_refs_are_left_to_the_handler(svc):
    made = run(svc, "nx_create_sheet", width="11 in", units="in")
    assert made["data"]["width"] == pytest.approx(11.0) and made["units"]["sheet_length"] == "in"
    metric = run(svc, "nx_create_sheet", width="11 in")
    assert metric["data"]["width"] == pytest.approx(279.4)
    # Gate 9 never fails on an unresolvable reference; the handler reports it.
    stale = run(svc, "nx_create_label", origin=[0, 0], sheet="sh:999999")
    assert stale["ok"] and stale["data"]["sheet"] is None


def test_non_sheet_tools_do_not_get_sheet_units(svc):
    ok = run(svc, "nx_create_block", size=1)
    assert "sheet_length" not in ok["units"] and not SPECS["nx_create_block"].sheet


def test_panel_summary_states_sheet_units():
    from nx_mcp.core import summary

    spec = SPECS["nx_create_label"]
    line = summary.render(spec, {"origin": [1, 2], "height": 0.2}, units={"sheet_length": "in"})
    assert line == "Label 0.2 in at (1, 2)"


# -- 2. view rollback and restore_id ----------------------------------------------------


def test_view_tool_returns_restore_id_for_what_it_kept(svc):
    body = svc.part.add("body", "B")
    ok = run(svc, "nx_set_look", objects=[ref(svc, body, "body")])
    assert ok["ok"] and ok["mutation_outcome"] == "committed"
    snapshot = svc.snapshots[ok["restore_id"]]
    assert snapshot["camera"] == {"scale": 1.0}
    assert snapshot["records"] == [(body, {"blanked": False, "color": 7})]
    assert svc.mark_names == []  # view tools set no undo mark


def test_failed_view_tool_restores_display_camera_and_highlights(svc):
    body = svc.part.add("body", "B")
    other = svc.part.add("body", "C")
    svc.highlighted = [other]
    bad = run(svc, "nx_set_look", objects=[ref(svc, body, "body")], fail_with="NX_BUILD_FAILED")
    assert bad["mutation_outcome"] == "rolled_back" and bad["error"]["code"] == "NX_BUILD_FAILED"
    assert (body.blanked, body.color) == (False, 7)
    assert svc.camera == {"scale": 1.0} and svc.highlighted == [other]
    assert "restore_id" not in bad and svc.snapshots == {}


def test_view_rollback_failure_is_restore_failed_partial(svc):
    body = svc.part.add("body", "B")
    svc.fail_restore = True
    bad = run(svc, "nx_set_look", objects=[ref(svc, body, "body")], fail_with="NX_BUILD_FAILED")
    assert bad["mutation_outcome"] == "partial"
    assert bad["error"]["code"] == "NX_RESTORE_FAILED"
    assert bad["error"]["category"] == "rollback_failed"
    assert bad["error"]["details"]["operation_error"]["code"] == "NX_BUILD_FAILED"


def test_view_tool_that_kept_nothing_reports_unknown(svc):
    body = svc.part.add("body", "B")
    bad = run(
        svc,
        "nx_set_look",
        objects=[ref(svc, body, "body")],
        fail_with="NX_BUILD_FAILED",
        keep=False,
    )
    assert bad["mutation_outcome"] == "unknown"


def test_untouched_view_failure_is_not_started(svc):
    svc.fail_camera = True
    body = svc.part.add("body", "B")
    bad = run(svc, "nx_set_look", objects=[ref(svc, body, "body")])
    assert bad["mutation_outcome"] == "not_started"


def test_restorable_needs_a_view_effect():
    from nx_mcp.core.catalogue import CatalogueError, make_spec

    with pytest.raises(CatalogueError, match="restorable requires effect 'view'"):
        make_spec(
            tools.nx_create_block,
            effect="model",
            label="Block",
            keywords=tools.KEYWORDS,
            domain="modeling",
            restorable=True,
            example={"size": 1},
        )


# -- 3. part ownership -------------------------------------------------------------------


@pytest.fixture
def production(tmp_path, monkeypatch):
    names = ("nx_create_block", "nx_save_other", "nx_create_steps")
    monkeypatch.setattr(evidence, "_CACHE", {n: {"v2406": {}} for n in names})
    return FakeServices(tmp_path, dev=False)


def test_not_granted_error_tells_the_agent_to_ask_the_engineer(production):
    bad = run(production, "nx_create_block", size=1)
    error = bad["error"]
    assert error["code"] == "NX_PART_NOT_GRANTED" and error["category"] == "engineer_control"
    assert error["retry"] == "ask_engineer" and bad["mutation_outcome"] == "not_started"
    assert "ask them in chat" in error["hint"] and "Allow agent to change a" in error["hint"]
    assert error["details"]["part"] == "a.prt"


def test_file_tools_check_the_part_they_save(production, tmp_path):
    engineer = FakePart(tmp_path / "engineer.prt")
    engineer.IsModified = True
    production.extra_parts.append(engineer)
    part_ref = ref(production, engineer, "part")
    bad = run(production, "nx_save_other", part=part_ref)
    assert bad["error"]["code"] == "NX_PART_NOT_GRANTED" and bad["error"]["field"] == "/part"
    assert engineer.IsModified  # nothing was saved
    production.own(engineer)
    assert run(production, "nx_save_other", part=part_ref)["ok"]


def test_development_mode_never_gates(svc, tmp_path):
    engineer = FakePart(tmp_path / "engineer.prt")
    svc.extra_parts.append(engineer)
    assert run(svc, "nx_save_other", part=ref(svc, engineer, "part"))["ok"]
    assert run(svc, "nx_create_block", size=1)["ok"]


# -- 4. long operations ---------------------------------------------------------------------


def test_checkpoints_write_progress(svc):
    ok = run(svc, "nx_create_steps", operation_id="op_steps_1", steps=3)
    assert ok["ok"]
    receipt = json.loads((svc.root / ".nx-mcp" / "operations" / "op_steps_1.json").read_text())
    assert receipt["progress"]["completed"] == 3 and receipt["progress"]["total"] == 3


def test_budget_overrun_is_cancelled_and_rolls_back(svc, monkeypatch):
    monkeypatch.setitem(tx_module.COST_BUDGET_SECONDS, "light", -1.0)
    bad = run(svc, "nx_create_steps", steps=3)
    error = bad["error"]
    assert error["code"] == "NX_CANCELLED" and error["category"] == "interrupted"
    assert error["details"]["reason"] == "budget" and error["details"]["budget_seconds"] == -1.0
    assert "Split the work" in error["hint"]
    assert bad["mutation_outcome"] == "rolled_back" and svc.part.objects == []


def test_budget_defaults_follow_the_cost_classes():
    assert tx_module.COST_BUDGET_SECONDS == {
        "light": 30.0,
        "heavy": 300.0,
        "long": 1800.0,
    }
    assert tx_module.DEFAULT_BUDGET_SECONDS == 300.0

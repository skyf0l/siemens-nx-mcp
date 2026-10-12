"""Gate 4s (core/switching.py, kit/switching.py) through the real kernel on the fake kit.

The kernel makes the call's target part current and restores the engineer's state:
``work`` mode (quiet work part, or ``SetWork`` for a component prototype under the
display), ``display`` mode (display switch with the application carried and restored),
``none`` (a read bound to its part). Covered: no switch and no NX call in solo or on the
work part, restore on failure, the post-handler work-part check, restore failure (home
state, warning, entry repair), deferred display-bound refreshers, the application carry,
the ``NX_MCP_SWITCH=display`` fallback, ``tx.scratch`` of a bound read and the switch log.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest

from nx_mcp.core import catalogue, kernel, switching
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Services, Tx
from tests.kit import fake_nx
from tests.kit.kernel import Result, services
from tests.kit.objects import Feature, Part
from tests.kit.parts import with_parts

MODELING, SHEET_METAL = "UG_APP_MODELING", "UG_APP_SBSM"


class Out(TypedDict, total=False):
    part: str
    work: str
    display: str
    application: str


SEEN: list = []  # what each handler saw: (tx.part, work, display, application)
BEHAVIOUR: dict = {}  # "fail": raise after the change; "leave": work part to leave


def _handler(tx: Tx) -> Out:
    parts = tx.session.Parts
    SEEN.append((tx.part, parts.Work, parts.Display, tx.session.ApplicationName))
    if tx.spec.effect == "model":
        tx.part.Features.append(Feature("THING"))
    if BEHAVIOUR.get("leave") is not None:
        parts.Work = BEHAVIOUR["leave"]
    if BEHAVIOUR.get("fail"):
        fail("NX_BUILD_FAILED", "the build failed (test)")
    if BEHAVIOUR.get("scratch"):
        with tx.scratch():
            SEEN.append(("scratch", parts.Work))
    return {"part": tx.part.Name}


def nx_create_thing(tx: Tx) -> Out:
    """Create a thing in the work part."""
    return _handler(tx)


def nx_get_thing(tx: Tx) -> Out:
    """Get a thing of the part."""
    return _handler(tx)


def nx_create_sheet_thing(tx: Tx) -> Out:
    """Create a sheet-metal thing on the displayed part."""
    return _handler(tx)


def nx_create_drawing_thing(tx: Tx) -> Out:
    """Create a drawing thing on the displayed part."""
    return _handler(tx)


def _spec(fn, effect, requires=("work_part",), **options):
    return make_spec(
        fn, effect=effect, label="Thing", keywords=("a", "b", "c"), domain="modeling",
        requires=requires, **options,
    )  # fmt: skip


MODEL = _spec(nx_create_thing, "model")
READ = _spec(nx_get_thing, "read")
SHEET_METAL_TOOL = _spec(
    nx_create_sheet_thing, "model", switch="display", requires=("work_part",
    "application:sheet_metal"),
)  # fmt: skip
DRAWING = make_spec(
    nx_create_drawing_thing, effect="model", label="Thing", keywords=("a", "b", "c"),
    domain="drawing", requires=("work_part",), target="refs",
)  # fmt: skip


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    SEEN.clear()
    BEHAVIOUR.clear()
    monkeypatch.delenv(switching.ENV, raising=False)


def _nx():
    """``work.prt`` displayed and work, ``plate.prt`` loaded in the background."""
    nx = fake_nx()
    root = Path(services(nx).root)
    with_parts(nx, root)
    nx.part.FullPath = str(root / "work.prt")
    nx.plate = _part(nx, "plate.prt")
    nx.session.strict_work = True  # NX: SetWork only under the display part
    return nx


def _part(nx, name):
    parts = nx.session.Parts
    work, display = parts.Work, parts.Display
    part = Part(nx.session, Path(services(nx).root) / name)
    parts.Work, parts.Display = work, display
    return part


def call(nx, spec, **args):
    return Result(kernel.run(spec, args, kernel.Context(services(nx))))


def _state(nx):
    return nx.session.Parts.Display, nx.session.Parts.Work


def _switches(nx):
    return [name for name, _ in nx.session.calls]


# ---------------------------------------------------------------------------
# No switch


def test_one_part_never_switches_and_makes_no_nx_call():
    nx = fake_nx()
    with_parts(nx, services(nx).root)
    for spec in (MODEL, READ, DRAWING):
        r = call(nx, spec)
        assert r.ok, r
    assert nx.session.calls == [] and "part" not in r.envelope
    assert len(nx.part.Features) == 2 and services(nx).switch_log() == []


def test_the_work_part_as_target_makes_no_nx_call():
    nx = _nx()
    for spec in (MODEL, READ, DRAWING):
        assert call(nx, spec, part="work").ok
    assert nx.session.calls == [] and services(nx).bound is None
    assert len(nx.part.Features) == 2 and not nx.plate.Features


# ---------------------------------------------------------------------------
# work: quiet, or SetWork for a component prototype under the display


def test_a_background_part_becomes_the_work_part_quietly_and_comes_back():
    nx = _nx()
    r = call(nx, MODEL, part="plate")
    assert r.ok and r.mutation_outcome == "committed", r
    assert r.part["file"] == "plate.prt" and r.part["from"] == "part"
    assert [(nx.plate, nx.plate, nx.part, MODELING)] == SEEN  # display never moved
    assert len(nx.plate.Features) == 1 and not nx.part.Features
    assert _switches(nx) == ["SetWorkPartQuietly", "SetWork"]
    assert _state(nx) == (nx.part, nx.part)
    entry = services(nx).switch_log()[-1]
    assert entry["mode"] == "work" and entry["primitive"] == "quiet" and entry["restored"]
    assert entry["switch_ms"] >= 0 and entry["restore_ms"] >= 0


def test_a_component_prototype_under_the_display_uses_set_work():
    nx = _nx()
    nx.attach(nx.part, nx.plate)  # plate is a component of the displayed work.prt
    r = call(nx, MODEL, part="plate")
    assert r.ok, r
    assert _switches(nx) == ["SetWork", "SetWork"] and _state(nx) == (nx.part, nx.part)
    assert services(nx).switch_log()[-1]["primitive"] == "SetWork"


def test_a_failing_call_rolls_back_and_restores():
    nx = _nx()
    BEHAVIOUR["fail"] = True
    r = call(nx, MODEL, part="plate")
    assert r.error["code"] == "NX_BUILD_FAILED" and r.mutation_outcome == "rolled_back", r
    assert not nx.plate.Features and _state(nx) == (nx.part, nx.part)
    assert r.part["file"] == "plate.prt"


def test_a_handler_that_leaves_another_work_part_fails_before_commit():
    nx = _nx()
    other = _part(nx, "other.prt")
    BEHAVIOUR["leave"] = other
    r = call(nx, MODEL, part="plate")
    assert r.error["code"] == "NX_WRONG_PART_CONTEXT" and r.mutation_outcome == "rolled_back", r
    assert "other.prt" in r.error["message"] and "plate.prt" in r.error["message"]
    assert not nx.plate.Features and _state(nx) == (nx.part, nx.part)
    assert not services(nx).history  # nothing committed


def test_a_refused_switch_is_not_started():
    nx = _nx()
    nx.uf.Assem = None  # this NX cannot make a background part work
    r = call(nx, MODEL, part="plate")
    assert r.error["code"] == "NX_WRONG_PART_CONTEXT" and r.mutation_outcome == "not_started", r
    assert SEEN == [] and _state(nx) == (nx.part, nx.part)


# ---------------------------------------------------------------------------
# Restore failure: home state, warning, entry repair


def test_a_restore_failure_after_commit_is_a_warning_with_the_home_state():
    nx = _nx()
    nx.session.fail_set_work = 1  # the restore's SetWork(work.prt) raises once
    r = call(nx, MODEL, part="plate")
    assert r.ok and r.mutation_outcome == "committed", r  # the change stays committed
    warning = next(w for w in r.warnings if w["code"] == "NX_UPDATE_WARNING")
    assert "could not fully restore" in warning["message"] and "home state" in warning["message"]
    assert _state(nx) == (nx.part, nx.part) and len(nx.plate.Features) == 1
    assert services(nx).history[-1]["part_id"] == services(nx).part_key(nx.plate)
    assert "_switch_leak" not in services(nx).__dict__  # clean again: nothing to repair


def test_a_state_nx_did_not_take_back_is_repaired_at_the_next_call():
    nx = _nx()
    quietly = nx.session.set_work_quietly
    used: list = []

    def once(tag):  # the switch works; the home state's quiet fallback then fails
        if used:
            raise RuntimeError("SetWorkPartQuietly refused (test)")
        used.append(tag)
        quietly(tag)

    nx.uf.Assem.SetWorkPartQuietly = once
    nx.session.fail_set_work = 3  # the restore and both home-state SetWork calls fail
    r = call(nx, MODEL, part="plate")
    assert r.ok and any("could not fully restore" in w["message"] for w in r.warnings), r
    assert _state(nx) == (nx.part, nx.plate)  # NX kept the quiet work part
    nx.session.calls.clear()
    again = call(nx, MODEL, part="work")
    assert again.ok, again
    assert any(w["code"] == "NX_NOTE" and switching.LEAK_NOTE in w["message"]
               for w in again.warnings)  # fmt: skip
    assert _state(nx) == (nx.part, nx.part) and len(nx.part.Features) == 1
    assert len(nx.plate.Features) == 1  # the repair ran before the call: built in work.prt


# ---------------------------------------------------------------------------
# display: drawings, the application carry


def test_a_display_call_switches_the_display_and_comes_back():
    nx = _nx()
    r = call(nx, DRAWING, part="plate")
    assert r.ok, r
    assert [(nx.plate, nx.plate, nx.plate, MODELING)] == SEEN
    assert _switches(nx) == ["show", "show"] and _state(nx) == (nx.part, nx.part)
    assert [s[0] for s in nx.session.shown] == ["retain", "retain"]  # undo marks kept
    assert services(nx).switch_log()[-1]["primitive"] == "display"


def test_the_display_fallback_switches_the_display_for_model_tools(monkeypatch):
    monkeypatch.setenv(switching.ENV, "display")
    nx = _nx()
    r = call(nx, MODEL, part="plate")
    assert r.ok and [(nx.plate, nx.plate, nx.plate, MODELING)] == SEEN, r
    assert _switches(nx) == ["show", "show"] and _state(nx) == (nx.part, nx.part)


def _sheet_metal(part):
    feature = Feature("SB_TAB(1)")
    feature.FeatureType = "SB_TAB"
    part.Features.append(feature)


def test_sheet_metal_is_entered_on_the_target_and_modeling_restored_without_dialog():
    nx = _nx()
    _sheet_metal(nx.plate)
    r = call(nx, SHEET_METAL_TOOL, part="plate")
    assert r.ok, r
    assert [(nx.plate, nx.plate, nx.plate, SHEET_METAL)] == SEEN
    assert nx.session.ApplicationName == MODELING and _state(nx) == (nx.part, nx.part)
    apps = [x for name, x in nx.session.calls if name == "ApplicationSwitchImmediate"]
    assert apps[0] == SHEET_METAL and apps[-1] == MODELING


def test_modeling_is_not_restored_on_a_shown_sheet_metal_part():
    nx = _nx()
    _sheet_metal(nx.part)  # the engineer's displayed part has sheet-metal features
    _sheet_metal(nx.plate)
    r = call(nx, SHEET_METAL_TOOL, part="plate")
    assert r.ok and nx.session.ApplicationName == SHEET_METAL, r
    assert any("stays in UG_APP_SBSM" in w["message"] for w in r.warnings)  # never a dialog


def test_without_a_switch_the_application_rule_refuses_as_before():
    nx = fake_nx()
    with_parts(nx, services(nx).root)
    r = call(nx, SHEET_METAL_TOOL)
    assert r.error["code"] == "NX_WRONG_APPLICATION" and nx.session.calls == [], r


# ---------------------------------------------------------------------------
# Gate 13: display-bound refreshers wait on a target that is not displayed


def _refreshers(nx, ran):
    def views(tx, objects):
        ran.append(("drawing_view", tx.part.Name))
        return []

    def constraints(tx, objects):
        ran.append(("assembly_constraint", tx.part.Name))
        return []

    services(nx).refreshers = lambda: {"drawing_view": views, "assembly_constraint": constraints}


def test_view_refreshers_are_deferred_on_a_background_part_with_a_note():
    nx = _nx()
    nx.plate.DrawingSheets = [Feature("SHEET 1")]  # the plate has a drawing sheet
    ran: list = []
    _refreshers(nx, ran)
    r = call(nx, MODEL, part="plate")
    assert r.ok, r
    assert ran == [("assembly_constraint", "plate")]  # views wait: not displayed
    assert any(w["code"] == "NX_NOTE" and "plate.prt is not displayed" in w["message"]
               for w in r.warnings)  # fmt: skip
    key = services(nx).part_key(nx.plate)
    assert key in switching.deferred(services(nx))
    ran.clear()
    drawing = call(nx, DRAWING, part="plate")  # displayed: the waiting views refresh
    assert drawing.ok and ran == [("drawing_view", "plate")], drawing
    assert key not in switching.deferred(services(nx))


def test_no_note_when_the_background_part_has_no_drawing():
    nx = _nx()
    ran: list = []
    _refreshers(nx, ran)
    r = call(nx, MODEL, part="plate")
    assert r.ok and not any(w["code"] == "NX_NOTE" for w in r.warnings), r


def test_refreshers_run_as_before_without_a_switch():
    nx = _nx()
    ran: list = []
    _refreshers(nx, ran)
    assert call(nx, MODEL, part="work").ok
    assert ran == [("assembly_constraint", "work"), ("drawing_view", "work")]


# ---------------------------------------------------------------------------
# none: a read is bound to its part


def test_a_read_is_bound_to_its_part_without_any_nx_call():
    nx = _nx()
    r = call(nx, READ, part="plate")
    assert r.ok and r.data["part"] == "plate", r
    assert [(nx.plate, nx.part, nx.part, MODELING)] == SEEN and nx.session.calls == []
    assert services(nx).bound is None and services(nx).work_part() is nx.part


def test_a_bound_reads_scratch_geometry_is_built_in_its_part():
    nx = _nx()
    BEHAVIOUR["scratch"] = True
    r = call(nx, READ, part="plate")
    assert r.ok, r
    assert ("scratch", nx.plate) in SEEN and _state(nx) == (nx.part, nx.part)
    assert _switches(nx) == ["SetWorkPartQuietly", "SetWork"]


def test_services_that_cannot_switch_refuse_a_named_part_as_in_p0():
    class Plain(type(services(fake_nx()))):
        def switch(self, part, mode, *, application=None):
            return Services.switch(self, part, mode, application=application)

    nx = _nx()
    nx._kit_services = Plain(nx)
    r = call(nx, MODEL, part="plate")
    assert r.error["code"] == "NX_WRONG_PART_CONTEXT" and r.mutation_outcome == "not_started"
    assert "does not switch" in r.error["message"] or "work part is" in r.error["message"]


def test_display_bound_arguments_pick_display_mode():
    specs = catalogue.load()
    sketch, refset = specs["nx_create_sketch"], specs["nx_create_reference_set"]
    assert sketch.switch_for({"placement": {"principal": "xy"}}) == "work"
    assert sketch.switch_for({"placement": {"path": {"path": ["e:1"]}}}) == "display"
    assert refset.switch_for({"name": "A"}) == "work"
    assert refset.switch_for({"name": "A", "component": "co:1"}) == "display"
    assert specs["nx_create_wave_link"].switch_for({}) == "display"
    assert specs["nx_update_assembly"].switch_for({}) == "display"
    assert specs["nx_get_sketch"].switch_for({}) == "work"  # sketch-activating read
    delete = specs["nx_delete_objects"]  # explosions, sections and drafting are displayed
    assert delete.switch_for({}, {"feature"}) == "work"
    assert delete.switch_for({}, {"feature", "explosion"}) == "display"
    for kind in ("section_view", "drawing_sheet", "dimension"):
        assert delete.switch_for({}, {kind}) == "display", kind
    assert specs["nx_get_camera"].switch_for({"part": "plate"}) == "none"  # a plain read
    extrude = specs["nx_create_extrude"]  # NX refuses a through-all extrude out of view
    assert extrude.switch_for({"end": 10}) == "work"
    assert extrude.switch_for({"end": {"type": "distance", "value": 10}}) == "work"
    assert extrude.switch_for({"end": {"type": "through_all"}}) == "display"
    assert extrude.switch_for({"end": 5, "start": {"type": "through_all"}}) == "display"
    edit = specs["nx_edit_component"]  # replace and make unique only in the shown assembly
    assert edit.switch_for({"components": ["o:1"], "name": "A"}) == "work"
    assert edit.switch_for({"components": ["o:1"], "replace_with": "b.prt"}) == "display"
    assert edit.switch_for({"components": ["o:1"], "make_unique": "c.prt"}) == "display"

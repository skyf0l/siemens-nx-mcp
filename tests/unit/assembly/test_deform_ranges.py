"""Deformed values on the deformable fake: unit strings, NX formula errors, a formula that
later leaves the recorded range (nx_update_assembly warns) and a mirror of a deformed
component (warned: the copy keeps the part's values)."""

from __future__ import annotations

from nx_mcp.kit import deform as kdeform
from tests.kit import run
from tests.kit.objects import NXException
from tests.unit.assembly.fakes import place
from tests.unit.assembly.test_deformable import (  # noqa: F401 - the fixture
    Instantiate,
    LiveExpr,
    define,
    deform,
    nx,
)


def test_a_unit_string_is_converted_to_the_expression_units(nx):  # noqa: F811
    assert define(nx).ok
    a = place(nx, nx.blk, "A")
    ok = deform(nx, a, ("extension", "7 cm"))
    assert ok.ok, ok
    assert ok.data["values"][0]["value"] == 70.0 and ok.data["values"][0]["formula"] == "70.0"
    angle = deform(nx, a, ("extension", "70 deg"))
    assert angle.error["field"] == "/values/0/value" and angle.mutation_outcome == "not_started"
    wide = deform(nx, a, ("extension", "20 cm"))
    assert wide.error["field"] == "/values/0/value" and wide.mutation_outcome == "not_started"


def test_an_nx_formula_error_is_an_invalid_formula(nx, monkeypatch):  # noqa: F811
    assert define(nx).ok
    a = place(nx, nx.blk, "A")

    def refuse(self):
        raise NXException(3270008, "The specified string contains a syntax error")

    monkeypatch.setattr(Instantiate, "Commit", refuse)
    nx.asm.Expressions.append(LiveExpr("hatch_ext", "90", lambda: nx.asm.Expressions))
    bad = deform(nx, a, ("extension", "hatch_ext + 10"))  # evaluates, then NX refuses it
    assert bad.error["code"] == "NX_INVALID_FORMULA", bad
    assert bad.error["field"] == "/values/0/value" and bad.error["nx_code"] == 3270008
    assert bad.mutation_outcome == "rolled_back"


def test_update_assembly_warns_about_a_value_driven_out_of_range(nx):  # noqa: F811
    assert define(nx).ok
    hatch = LiveExpr("hatch_ext", "90", lambda: nx.asm.Expressions)
    nx.asm.Expressions.append(hatch)
    a = place(nx, nx.blk, "A")
    assert deform(nx, a, ("extension", "hatch_ext + 10")).ok
    assert kdeform.out_of_range(nx.tx, nx.asm) == []
    hatch._rhs = "300"  # an engineer's edit: the deformed value is now 310 (max 150)
    rows = kdeform.out_of_range(nx.tx, nx.asm)
    assert [(r["component"]["id"], r["expression"], r["value"]) for r in rows] == [
        (a.ref, "extension", 310.0)
    ]
    ok = run(nx, "nx_update_assembly", part="asm")
    assert ok.ok, ok
    assert any("outside its range" in w["message"] for w in ok.warnings), ok.warnings


def test_the_deformed_bodies_of_a_component(nx):  # noqa: F811
    assert define(nx).ok
    a = place(nx, nx.blk, "A")
    ok = deform(nx, a, ("extension", 120))
    assert ok.ok, ok
    assert [nx.ref_of(b, "body") for b in kdeform.bodies_of(nx.tx, a)] == [
        row["id"] for row in ok.data["bodies"]
    ]
    other = place(nx, nx.blk, "B")
    assert kdeform.bodies_of(nx.tx, other) == []


def test_a_mirror_of_a_deformed_component_warns(nx):  # noqa: F811
    assert define(nx).ok
    a = place(nx, nx.blk, "A", (40, 0, 0))
    assert deform(nx, a, ("extension", 120)).ok
    ok = run(nx, "nx_create_component_mirror", part="asm", components=[a.ref], plane="yz")
    assert ok.ok, ok
    assert ok.data["components"][0]["mirrored"]["name"] == "A_MIRROR"
    assert any("is deformed" in w["message"] for w in ok.warnings), ok.warnings

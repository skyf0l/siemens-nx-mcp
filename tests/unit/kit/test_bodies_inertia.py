"""kit.bodies (owner/scope bodies) and kit.measure inertia/WCS helpers."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import bodies, measure
from tests.kit import fake_nx
from tests.kit.objects import Body, Component, Feature, matrix, point


def _assembly(nx):
    root = Component("ROOT")
    nx.part.ComponentAssembly = NS(RootComponent=root)
    a = Component("A", [Body("a", [0, 0, 0, 1, 1, 1])], parent=root)
    b = Component("B", [Body("b", [2, 0, 0, 3, 1, 1])], parent=root)
    nested = Component("N", [Body("n", [4, 0, 0, 5, 1, 1])], parent=b)
    return a, b, nested


def test_scope_and_owner_bodies_follow_suppression_and_nesting():
    nx = fake_nx()
    nx.tx.checkpoint = lambda *_: None
    own = nx.block(1, 1, 1)
    a, b, nested = _assembly(nx)
    assert bodies.scope(nx.tx, "part") == [own]
    assert len(bodies.scope(nx.tx, "auto")) == 4
    assert len(bodies.of(nx.tx, b)) == 2  # B and its nested child
    b.IsSuppressed = True
    assert bodies.of(nx.tx, nested) == []  # a suppressed parent hides the child
    assert len(bodies.scope(nx.tx, "assembly")) == 2
    feature = Feature("EXTRUDE", [own, own])
    assert bodies.of(nx.tx, feature) == [own]
    with pytest.raises(NXError) as bad:
        bodies.scope(nx.tx, "galaxy")
    assert bad.value.field == "/scope"


def test_solids_and_require():
    sheet = Body("s")
    sheet.IsSolidBody = False
    with pytest.raises(NXError) as error:
        bodies.solids([Body(), sheet], field="/bodies")
    assert error.value.code == "NX_WRONG_GEOMETRY"
    with pytest.raises(NXError) as empty:
        bodies.require([])
    assert empty.value.code == "NX_NOT_FOUND"


def test_inertia_uses_centroidal_values_product_signs_and_part_units():
    """UF AskMassProps3d: centroid 3-5 (m, WCS), centroidal moments 12-14, products 19-21."""
    nx = fake_nx()
    values = [float(i) for i in range(47)]
    nx.uf.Modeling.AskMassProps3d = Mock(return_value=(values, [0.0] * 11))
    nx.part.WCS = NS(CoordinateSystem=NS(Origin=point(1, 2, 3), Orientation=NS(Element=matrix())))
    out = measure.inertia(nx.tx, [Body()])
    k = 1000.0  # m -> mm
    assert out["mass"] == 2
    assert out["center"] == pytest.approx([1 + 3 * k, 2 + 4 * k, 3 + 5 * k])
    flat = [v for row in out["inertia"] for v in row]
    assert flat == pytest.approx([k * k * v for v in (12, -19, -21, -19, 13, -20, -21, -20, 14)])
    assert out["principal"] == pytest.approx([31 * k * k, 32 * k * k, 33 * k * k])
    assert measure.wcs_is_absolute(nx.tx)
    nx.part.WCS.CoordinateSystem.Orientation.Element = matrix((0, 1, 0), (-1, 0, 0), (0, 0, 1))
    assert not measure.wcs_is_absolute(nx.tx)


def test_the_wcs_is_the_displayed_parts():
    """NX refuses WCS of a part that is not displayed (a read bound to a background part,
    gate 4s); UF reports in the displayed part's WCS anyway."""
    nx = fake_nx()
    shown = NS(WCS=NS(CoordinateSystem=NS(Origin=point(7, 8, 9), Orientation=NS(Element=matrix()))))
    nx.session.Parts.Display = shown
    nx.part.WCS = None  # what a background part gives
    origin, _r = measure.wcs(nx.tx)
    assert origin == [7, 8, 9]
    nx.session.Parts.Display = None  # nothing displayed: the part's own
    nx.part.WCS = shown.WCS
    assert measure.wcs(nx.tx)[0] == [7, 8, 9]


def test_expect_sound_skips_known_faults_and_scale_note_follows_part_units():
    nx = fake_nx()
    good, bad = nx.block(1, 1, 1), nx.block(2, 2, 2)
    faults = {int(bad.Tag): (2, [875316, 1], [bad.Tag])}
    nx.uf.Modeling.AskBodyConsistency = lambda tag: faults.get(int(tag), (0, [], []))
    assert bodies.faulty(nx.tx, [good, bad]) == {int(bad.Tag)}
    bodies.expect_sound(nx.tx, [good, bad], known={int(bad.Tag)})  # faulty before: passes
    with pytest.raises(NXError) as error:
        bodies.expect_sound(nx.tx, [good, bad])
    assert error.value.code == "NX_BUILD_FAILED" and len(error.value.details["faults"]) == 2
    nx.uf.Modeling.AskBodyConsistency = Mock(side_effect=RuntimeError("no UF"))
    bodies.expect_sound(nx.tx, [bad])  # UF cannot tell: no failure
    nx.uf.ModlGeneral = NS(AskBoundingBox=lambda tag: [0, 0, 0, 12_000, 1, 1])
    assert "12000 mm" in bodies.scale_note(nx.tx, [good])
    nx.uf.ModlGeneral = NS(AskBoundingBox=lambda tag: [0, 0, 0, 9_000, 1, 1])
    assert bodies.scale_note(nx.tx, [good]) is None and bodies.scale_note(nx.tx, []) is None


def _examine(failed: dict, destroyed: list):
    """A fake ExamineGeometry whose body face intersection check fails ``failed[tag]``
    faces of a body."""
    seen: list = []

    def create():
        return NS(
            ClearAllChecks=lambda: None,
            SetCheck=lambda check: None,
            ObjectsToExamine=NS(Add=lambda objs: seen.extend(objs)),
            Examine=lambda: None,
            GetFailedObjects=lambda check: [object()] * failed.get(int(seen[-1].Tag), 0),
            Destroy=lambda: destroyed.append(True),
        )

    return NS(CreateExamineGeometryObject=create)


def test_expect_no_clash_fails_self_overlapping_bodies_and_skips_known_ones():
    nx = fake_nx()
    good, bad = nx.block(1, 1, 1), nx.block(2, 2, 2)
    nx.nx.GeometricAnalysis = NS(ExamineGeometry=NS(Check=NS(BodyFaceIntersections="faces")))
    destroyed: list = []
    nx.part.AnalysisManager = _examine({int(bad.Tag): 144}, destroyed)
    assert bodies.face_clashes(nx.tx, bad) == 144 and bodies.face_clashes(nx.tx, good) == 0
    assert bodies.clashing(nx.tx, [good, bad]) == {int(bad.Tag)}
    bodies.expect_no_clash(nx.tx, [good, bad], known={int(bad.Tag)})  # overlapping before
    with pytest.raises(NXError) as error:
        bodies.expect_no_clash(nx.tx, [good, bad])
    assert error.value.code == "NX_BUILD_FAILED"
    assert error.value.details == {"check": "body_health", "face_intersections": 144}
    assert len(destroyed) == 7  # every examine object is destroyed
    nx.part.AnalysisManager = NS(CreateExamineGeometryObject=Mock(side_effect=RuntimeError))
    bodies.expect_no_clash(nx.tx, [bad])  # NX cannot tell: no failure

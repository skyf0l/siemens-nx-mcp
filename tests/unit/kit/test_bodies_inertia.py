"""kit.bodies (owner/scope bodies) and kit.measure inertia/WCS helpers."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import bodies, massprops, measure
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
    """UF AskMassProps3d: centroid 3-5 (m, WCS), centroidal moments 12-14, products
    yz, xz, xy at 19, 20, 21 (negated in the tensor)."""
    nx = fake_nx()
    values = [float(i) for i in range(47)]
    nx.uf.Modeling.AskMassProps3d = Mock(return_value=(values, [0.0] * 11))
    nx.part.WCS = NS(CoordinateSystem=NS(Origin=point(1, 2, 3), Orientation=NS(Element=matrix())))
    out = measure.inertia(nx.tx, [Body()])
    k = 1000.0  # m -> mm
    assert out["mass"] == 2
    assert out["center"] == pytest.approx([1 + 3 * k, 2 + 4 * k, 3 + 5 * k])
    flat = [v for row in out["inertia"] for v in row]
    assert flat == pytest.approx([k * k * v for v in (12, -21, -20, -21, 13, -19, -20, -19, 14)])
    assert out["principal"] == pytest.approx([31 * k * k, 32 * k * k, 33 * k * k])
    assert measure.wcs_is_absolute(nx.tx)
    nx.part.WCS.CoordinateSystem.Orientation.Element = matrix((0, 1, 0), (-1, 0, 0), (0, 0, 1))
    assert not measure.wcs_is_absolute(nx.tx)


def _offset_blocks():
    """Two offset blocks of 1000 kg/m^3: 100x50x20 mm at the origin and a 10 mm cube
    at x 200..210, y 30..40, z 0..10 (NX 2406 read the same set live)."""
    items = [
        (0.1, (50.0, 25.0, 10.0), (100.0, 50.0, 20.0)),
        (0.001, (205.0, 35.0, 5.0), (10.0,) * 3),
    ]
    mass = sum(m for m, _c, _s in items)
    centre = [sum(m * c[i] for m, c, _s in items) / mass for i in range(3)]
    moments = [0.0, 0.0, 0.0]
    products = {"yz": 0.0, "xz": 0.0, "xy": 0.0}  # sum(m*a*b), positive
    for m, c, s in items:
        d = [c[i] - centre[i] for i in range(3)]
        own = [m * (s[1] ** 2 + s[2] ** 2) / 12, m * (s[0] ** 2 + s[2] ** 2) / 12,
               m * (s[0] ** 2 + s[1] ** 2) / 12]  # fmt: skip
        for i in range(3):
            moments[i] += own[i] + m * (sum(v * v for v in d) - d[i] ** 2)
        products["yz"] += m * d[1] * d[2]
        products["xz"] += m * d[0] * d[2]
        products["xy"] += m * d[0] * d[1]
    return mass, centre, moments, products


def test_inertia_eigenvalues_are_the_principal_moments():
    """The tensor's products sit where NX puts them: its eigenvalues are the principal
    moments NX reports (an asymmetric two-body set, all three products non-zero)."""
    nx = fake_nx()
    mass, centre, moments, products = _offset_blocks()
    mm2 = 1e-6  # kg mm^2 -> kg m^2
    tensor = [
        [moments[0], -products["xy"], -products["xz"]],
        [-products["xy"], moments[1], -products["yz"]],
        [-products["xz"], -products["yz"], moments[2]],
    ]
    principal = massprops.principal_moments(tensor)
    values = [0.0] * 47
    values[2] = mass
    values[3:6] = [c * 1e-3 for c in centre]
    values[12:15] = [v * mm2 for v in moments]
    values[19:22] = [products["yz"] * mm2, products["xz"] * mm2, products["xy"] * mm2]
    values[31:34] = [v * mm2 for v in principal]
    nx.uf.Modeling.AskMassProps3d = Mock(return_value=(values, [0.0] * 11))
    nx.part.WCS = NS(CoordinateSystem=NS(Origin=point(0, 0, 0), Orientation=NS(Element=matrix())))
    out = measure.inertia(nx.tx, [Body()])
    assert out["inertia"][0][1] == pytest.approx(-1.534653465, rel=1e-6)  # NX 2406 live
    assert out["inertia"][0][2] == pytest.approx(0.767326733, rel=1e-6)
    assert out["inertia"][1][2] == pytest.approx(0.049504950, rel=1e-6)
    assert massprops.principal_moments(out["inertia"]) == pytest.approx(
        sorted(out["principal"]), rel=1e-9
    )


@pytest.mark.parametrize(("units", "k"), [("mm", 1000.0), ("in", 1 / 0.0254)])
def test_solid_props_reads_one_body_in_part_units(units, k):
    """UF AskMassProps3d of one solid: area at 0 (m^2), volume at 1 (m^3)."""
    nx = fake_nx(units=units)
    values = [0.0] * 47
    values[0], values[1] = 2.8e-3, 8e-6
    nx.uf.Modeling.AskMassProps3d = Mock(return_value=(values, [0.0] * 11))
    body = Body()
    out = measure.solid_props(nx.tx, body)
    assert out == pytest.approx({"volume": 8e-6 * k**3, "area": 2.8e-3 * k**2, "mass": 0.0})
    tags, count, kind, flag = nx.uf.Modeling.AskMassProps3d.call_args.args[:4]
    assert tags == [body.Tag] and count == 1 and (kind, flag) == (1, 4)


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


def test_bodies_over_the_face_limit_are_not_examined_for_self_overlap(monkeypatch):
    """Examine Geometry cannot be interrupted and runs for an hour on a body of about
    17,600 faces: such bodies are skipped, with a note after a build."""
    nx = fake_nx()
    small, big = nx.block(1, 1, 1), nx.block(2, 2, 2)
    big.faces = big.faces * 3  # 18 faces
    nx.nx.GeometricAnalysis = NS(ExamineGeometry=NS(Check=NS(BodyFaceIntersections="faces")))
    destroyed: list = []
    nx.part.AnalysisManager = _examine({int(small.Tag): 4, int(big.Tag): 4}, destroyed)
    monkeypatch.setattr(bodies, "CLASH_FACES", 10)
    assert bodies.face_count(big) == 18 and bodies.examinable(small)
    assert not bodies.examinable(big)
    assert bodies.clashing(nx.tx, [small, big]) == {int(small.Tag)} and len(destroyed) == 1
    note = bodies.expect_no_clash(nx.tx, [big])  # not examined: no failure, a note
    assert len(destroyed) == 1
    assert note is not None and "clash: true" in note
    assert note.startswith("Self-overlap check skipped: the body has 18 faces (over 10)")
    assert bodies.expect_no_clash(nx.tx, [small], known={int(small.Tag)}) is None

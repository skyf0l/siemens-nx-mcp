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

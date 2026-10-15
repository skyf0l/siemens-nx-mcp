"""Wrap geometry on the shared fake, through the real kernel."""

import pytest

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body


def _wraps(nx, box, volume=None):
    def hook(_b):
        return feature(nx, "WRAP_GEOMETRY", [new_body(nx, box, volume)])

    nx.on_commit("WrapGeometryBuilder", hook)


def test_wrap_adds_every_body_and_reads_the_envelope_back():
    nx = modeling_nx()
    a = nx.block(10, 10, 10)
    b = nx.block(10, 10, 10, origin=(30, 0, 0))
    _wraps(nx, [-0.1, -0.1, -0.1, 40.1, 10.1, 10.1])
    ok = run(nx, "nx_create_wrap_geometry", bodies=[a.ref, b.ref], offset=1, close_gaps="beveled")
    assert ok.ok, ok
    builder = nx.last("WrapGeometryBuilder")
    assert [c[0][0] for c in builder.Geometry.Add.calls] == [a, b]
    gaps = nx.nx.Features.WrapGeometryBuilder.CloseGapType
    assert builder.CloseGaps is gaps.Beveled and builder.DistTol == pytest.approx(0.1)
    assert ok.data["inputs_volume"] == 2000 and ok.data["volume"] > 4000
    assert len(ok.data["bodies"]) == 1


def test_an_envelope_that_misses_an_input_rolls_back():
    nx = modeling_nx()
    a = nx.block(10, 10, 10)
    b = nx.block(10, 10, 10, origin=(30, 0, 0))
    _wraps(nx, [0, 0, 0, 20, 10, 10], volume=5000)
    bad = run(nx, "nx_create_wrap_geometry", bodies=[a.ref, b.ref])
    assert bad.error["details"]["check"] == "bounds", bad
    assert bad.mutation_outcome == "rolled_back"


def test_wrap_arguments_are_checked_first():
    nx = modeling_nx()
    a = nx.block(10, 10, 10)
    neg = run(nx, "nx_create_wrap_geometry", bodies=[a.ref], offset=-1)
    assert neg.error["field"] == "/offset" and neg.mutation_outcome == "not_started"
    tol = run(nx, "nx_create_wrap_geometry", bodies=[a.ref], tolerance=0)
    assert tol.error["field"] == "/tolerance"


def test_a_tolerance_too_large_for_the_inputs_names_tolerance():
    nx = modeling_nx()
    a = nx.block(10, 10, 10)
    nx.refuse("WrapGeometryBuilder", code=671360,
              message="Geometry to wrap is coplanar within given tolerance.")  # fmt: skip
    bad = run(nx, "nx_create_wrap_geometry", bodies=[a.ref], tolerance=20)
    assert bad.error["field"] == "/tolerance" and "tenth" in bad.error["hint"], bad
    assert bad.mutation_outcome == "rolled_back"


def test_split_planes_go_to_the_planes_list():
    nx = modeling_nx()
    a = nx.block(10, 10, 10)
    b = nx.block(10, 10, 10, origin=(30, 0, 0))
    _wraps(nx, [-0.1, -0.1, -0.1, 40.1, 10.1, 10.1], volume=2100)
    ok = run(nx, "nx_create_wrap_geometry", bodies=[a.ref, b.ref],
             split_planes=["yz", {"origin": [20, 0, 0], "normal": [1, 0, 0]}])  # fmt: skip
    assert ok.ok, ok
    assert len(nx.last("WrapGeometryBuilder").PlanesList.Append.calls) == 2
    bad = run(nx, "nx_create_wrap_geometry", bodies=[a.ref], split_planes=[{"origin": [0, 0, 0],
              "normal": [0, 0, 0]}])  # fmt: skip
    assert (
        bad.error["field"].startswith("/split_planes/0") and bad.mutation_outcome == "not_started"
    )

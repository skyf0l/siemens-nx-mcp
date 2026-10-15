"""A sheet revolve refused for a profile curve on the axis says so (no combine hint)."""

from tests.kit import run
from tests.unit.modeling.fakes import modeling_nx
from tests.unit.modeling.test_features import _lines

ON_AXIS = [((0, 0, 0), (5, 0, 0)), ((5, 0, 0), (5, 10, 0)), ((5, 10, 0), (0, 10, 0)),
           ((0, 10, 0), (0, 0, 0))]  # fmt: skip


def test_sheet_revolve_with_a_curve_on_the_axis_names_it():
    nx = modeling_nx()
    sk = nx.sketch()
    sk.geometry = _lines(nx, ON_AXIS)
    nx.refuse("RevolveBuilder", code=671623, message="Unable to create revolved body")
    bad = run(nx, "nx_create_revolve", sketch=sk.ref, axis={"principal": "y"}, sheet=True)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and "on the axis" in bad.error["message"]
    assert "construction" in bad.error["hint"] and "combine" not in bad.error["hint"], bad


def test_a_solid_revolve_refusal_gives_no_combine_hint_without_combine():
    nx = modeling_nx()
    sk = nx.sketch()
    nx.refuse("RevolveBuilder", code=671623, message="Unable to create revolved body")
    bad = run(nx, "nx_create_revolve", sketch=sk.ref, axis={"principal": "y"})
    assert bad.error["code"] == "NX_BUILD_FAILED" and "combine" not in bad.error["hint"], bad

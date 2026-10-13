"""nx_measure_section_inertia on the fake kit: hand values for a rectangle, a tube and an
L section; holes, open chains, the NX cross-check and refusals."""

import math

import pytest

from nx_mcp.domains.inspection import section_inertia
from tests.kit import run
from tests.kit.objects import NS
from tests.unit.inspection.fakes import arc_spec, inspection_nx


def rect_xy(x0, y0, x1, y1, z=50.0, reverse=False):
    p = [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)]
    lines = [("line", list(p[i]), list(p[(i + 1) % 4])) for i in range(4)]
    if reverse:
        lines = [("line", b, a) for _, a, b in reversed(lines)]
    return lines


def test_a_rectangle_gives_bh3_over_12_about_its_centroid():
    nx = inspection_nx()
    body = nx.block(20, 40, 100)
    nx.section_cuts[int(body.Tag)] = rect_xy(0, 0, 20, 40)
    curves = len(nx.part.Curves)
    ok = run(nx, "nx_measure_section_inertia", plane="xy", offset=50)
    assert ok.ok and ok.mutation_outcome == "not_applicable", ok
    assert len(nx.part.Curves) == curves  # the temporary curves were undone
    d = ok.data
    assert d["area"] == pytest.approx(800)
    assert d["centroid"] == pytest.approx([10, 20, 50]) and d["centroid_uv"] == pytest.approx(
        [10, 20]
    )
    assert d["i_u"] == pytest.approx(20 * 40**3 / 12) and d["i_v"] == pytest.approx(40 * 20**3 / 12)
    assert d["i_uv"] == pytest.approx(0, abs=1e-6) and d["polar"] == pytest.approx(
        d["i_u"] + d["i_v"]
    )
    assert d["extreme_v"] == pytest.approx([20, 20]) and d["extreme_u"] == pytest.approx([10, 10])
    assert d["modulus_u"] == pytest.approx(20 * 40**2 / 6)
    assert d["modulus_v"] == pytest.approx(40 * 20**2 / 6)
    assert d["gyration_u"] == pytest.approx(40 / math.sqrt(12))
    principal = d["principal"]
    assert principal["i_max"] == pytest.approx(d["i_u"]) and principal["angle"] == pytest.approx(0)
    assert principal["axis_max"] == pytest.approx([1, 0, 0])
    assert principal["axis_min"] == pytest.approx([0, 1, 0])
    assert principal["modulus_max"] == pytest.approx(d["modulus_u"])
    assert d["loops"] == 1 and d["open_chains"] == 0 and "nx" not in d


def test_a_tube_gives_pi_d4_minus_d4_over_64():
    nx = inspection_nx()
    body = nx.block(50, 50, 100, origin=(75, -25, 0))
    nx.section_cuts[int(body.Tag)] = [
        arc_spec((100, 0, 50), 25, 0, 2 * math.pi),
        arc_spec((100, 0, 50), 15, 0, 2 * math.pi),
    ]
    d = run(nx, "nx_measure_section_inertia", plane="xy", offset=50).data
    moment = math.pi * (50**4 - 30**4) / 64
    assert d["area"] == pytest.approx(math.pi * (25**2 - 15**2), rel=1e-12)
    assert d["i_u"] == pytest.approx(moment, rel=1e-12)
    assert d["i_v"] == pytest.approx(moment, rel=1e-12)
    assert d["polar"] == pytest.approx(2 * moment, rel=1e-12)
    assert d["modulus_u"] == pytest.approx(moment / 25, rel=1e-12)
    assert d["centroid"] == pytest.approx([100, 0, 50])
    assert d["extreme_u"] == pytest.approx([25, 25])


def test_an_l_section_has_a_product_and_turned_principal_axes():
    nx = inspection_nx()
    body = nx.block(50, 30, 10)
    pts = [(0, 0), (50, 0), (50, 5), (5, 5), (5, 30), (0, 30)]
    nx.section_cuts[int(body.Tag)] = [
        ("line", [*pts[i], 0.0], [*pts[(i + 1) % 6], 0.0]) for i in range(6)
    ]
    d = run(nx, "nx_measure_section_inertia", origin=[0, 0, 0], normal=[0, 0, 1]).data
    assert d["area"] == pytest.approx(375) and d["centroid"] == pytest.approx([17.5, 7.5, 0])
    assert d["i_uv"] == pytest.approx(-28125)
    i_u, i_v, i_uv = d["i_u"], d["i_v"], d["i_uv"]
    p = d["principal"]
    assert p["i_max"] + p["i_min"] == pytest.approx(i_u + i_v)
    assert p["i_max"] * p["i_min"] == pytest.approx(i_u * i_v - i_uv**2)
    a = math.radians(p["angle"])  # I about the axis at the angle is i_max
    about = i_u * math.cos(a) ** 2 + i_v * math.sin(a) ** 2 - 2 * i_uv * math.sin(a) * math.cos(a)
    assert about == pytest.approx(p["i_max"])
    assert p["axis_max"] == pytest.approx([math.cos(a), math.sin(a), 0])


def test_a_hollow_rectangle_subtracts_the_hole_in_any_winding():
    nx = inspection_nx()
    body = nx.block(20, 40, 100)
    nx.section_cuts[int(body.Tag)] = rect_xy(0, 0, 20, 40) + rect_xy(5, 5, 15, 35)
    d = run(nx, "nx_measure_section_inertia", plane="xy", offset=50).data
    assert d["area"] == pytest.approx(800 - 300) and d["loops"] == 2
    assert d["i_u"] == pytest.approx((20 * 40**3 - 10 * 30**3) / 12)
    nx.section_cuts[int(body.Tag)] = rect_xy(0, 0, 20, 40, reverse=True) + rect_xy(5, 5, 15, 35)
    again = run(nx, "nx_measure_section_inertia", plane="xy", offset=50).data
    assert again["i_u"] == pytest.approx(d["i_u"])


def test_far_from_the_origin_keeps_its_precision():
    nx = inspection_nx()
    body = nx.block(20, 40, 100, origin=(1e5, 2e5, 0))
    nx.section_cuts[int(body.Tag)] = rect_xy(1e5, 2e5, 1e5 + 20, 2e5 + 40)
    d = run(nx, "nx_measure_section_inertia", plane="xy", offset=50).data
    assert d["i_u"] == pytest.approx(20 * 40**3 / 12, rel=1e-9)
    assert d["centroid_uv"] == pytest.approx([1e5 + 10, 2e5 + 20])


def test_open_chains_warn_and_an_empty_cut_returns_no_moments():
    nx = inspection_nx()
    body = nx.block(20, 40, 100)
    nx.section_cuts[int(body.Tag)] = rect_xy(0, 0, 20, 40) + [
        ("line", [30.0, 0.0, 50.0], [31.0, 0.0, 50.0])
    ]
    ok = run(nx, "nx_measure_section_inertia", plane="xy", offset=50)
    assert ok.data["open_chains"] == 1 and ok.data["area"] == pytest.approx(800)
    assert "open section chains" in ok.warnings[0]["message"]
    nx.section_cuts.clear()  # the plane misses the body
    empty = run(nx, "nx_measure_section_inertia", plane="xy", offset=500)
    assert empty.ok and empty.data["loops"] == 0 and "i_u" not in empty.data
    assert "no loops" in empty.warnings[0]["message"]


def test_nx_cross_check_is_returned_and_a_difference_warns():
    nx = inspection_nx()
    body = nx.block(20, 40, 100)
    nx.section_cuts[int(body.Tag)] = rect_xy(0, 0, 20, 40)
    calls = []

    def inertia(curves, units, solid, thickness, wcs):
        calls.append((len(curves), [getattr(u, "Name", u) for u in units], solid))
        return (None, 106666.6666666667, 26666.66666666667, None, None, 120.0, 800.0, None)

    nx.session.Measurement = NS(GetSectionInertia=inertia)
    ok = run(nx, "nx_measure_section_inertia", plane="xy", offset=50)
    assert ok.data["nx"] == pytest.approx({"area": 800, "i_max": 106666.667, "i_min": 26666.667})
    assert calls == [(4, list(section_inertia.UNIT_NAMES["mm"]), True)] and not ok.warnings

    def off(*_args):
        return (None, 1.0, 1.0, None, None, 1.0, 801.0, None)

    nx.session.Measurement = NS(GetSectionInertia=off)
    warned = run(nx, "nx_measure_section_inertia", plane="xy", offset=50)
    assert "NX's own section inertia gives area 801" in warned.warnings[0]["message"]


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({}, "/plane"),
        ({"plane": "xy", "normal": [0, 0, 1]}, "/normal"),
        ({"normal": [0, 0, 1], "offset": 2}, "/offset"),
        ({"normal": [0, 0, 0]}, "/normal"),
    ],
)
def test_bad_arguments_are_refused_before_nx(args, field):
    nx = inspection_nx()
    nx.block(10, 10, 10)
    bad = run(nx, "nx_measure_section_inertia", **args)
    assert bad.error["field"] == field and bad.mutation_outcome == "not_applicable"
    assert nx.planes == []

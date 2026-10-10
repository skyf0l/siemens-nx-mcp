"""nx_measure_section on the fake kit: loops, areas, radial profiles, the model unchanged."""

import math

import pytest

from tests.kit import run
from tests.unit.inspection.fakes import arc_spec, inspection_nx


def rect(x0, z0, x1, z1, flip=False):
    """A rectangle in the xz plane as four lines (some reversed, like NX's cut)."""
    p = [(x0, 0.0, z0), (x1, 0.0, z0), (x1, 0.0, z1), (x0, 0.0, z1)]
    lines = [("line", list(p[i]), list(p[(i + 1) % 4])) for i in range(4)]
    if flip:
        lines[1] = ("line", lines[1][2], lines[1][1])
        lines.reverse()
    return lines


def ring_section(nx):
    """The cut of a revolved nested-rectangle profile (outer 5..20 x 0..40, void 8..15 x
    10..30), both sides of the Z axis."""
    body = nx.block(40, 40, 40, origin=(-20, -20, 0))
    nx.section_cuts[int(body.Tag)] = (
        rect(5, 0, 20, 40)
        + rect(8, 10, 15, 30, flip=True)
        + rect(-20, 0, -5, 40, flip=True)
        + rect(-15, 10, -8, 30)
    )
    return body


def test_loops_areas_and_roles_with_the_model_unchanged():
    nx = inspection_nx()
    body = ring_section(nx)
    curves = len(nx.part.Curves)
    ok = run(nx, "nx_measure_section", plane="xz")
    assert ok.ok and ok.mutation_outcome == "not_applicable", ok
    assert len(nx.part.Curves) == curves  # the temporary curves were undone
    assert nx.planes == [([0.0, 0.0, 0.0], [0.0, -1.0, 0.0])]
    data = ok.data
    assert data["frame"]["u"] == [1.0, 0.0, 0.0] and data["frame"]["v"] == [0.0, 0.0, 1.0]
    assert data["area"] == pytest.approx(2 * (15 * 40 - 7 * 20))
    loops = sorted(data["loops"], key=lambda r: (r["min"][0], r["area"]))
    assert [(r["role"], r["closed"]) for r in loops] == [
        ("outer", True), ("hole", True), ("outer", True), ("hole", True)
    ]  # fmt: skip
    assert [r["area"] for r in loops] == pytest.approx([600, 140, 600, 140])
    assert loops[2]["min"] == pytest.approx([5, 0]) and loops[2]["max"] == pytest.approx([20, 40])
    assert all(r["body"] == body.ref for r in loops)
    segment = loops[2]["segments"][0]
    assert segment["type"] == "line" and len(loops[2]["segments"]) == 4


def flat(spans):
    return [v for span in spans for v in span]


def test_radial_profile_gives_the_material_spans_per_station():
    nx = inspection_nx()
    ring_section(nx)
    ok = run(nx, "nx_measure_section", radial_axis="z", at=[5, 20, 40.5])
    assert ok.ok, ok
    low, mid, off = ok.data["profile"]
    assert flat(low["spans"]) == pytest.approx([5, 20]) and low["min"] == pytest.approx(5)
    assert flat(mid["spans"]) == pytest.approx([5, 8, 15, 20]) and mid["max"] == pytest.approx(20)
    assert off == {"at": 40.5, "min": None, "max": None, "spans": []}
    even = run(nx, "nx_measure_section", radial_axis="z", stations=4).data["profile"]
    assert [s["at"] for s in even] == pytest.approx([5, 15, 25, 35])
    assert flat(even[1]["spans"]) == pytest.approx([5, 8, 15, 20])


def test_arcs_have_exact_areas_and_boxes():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    xz = {"x": (1.0, 0.0, 0.0), "y": (0.0, 0.0, 1.0)}
    nx.section_cuts[int(body.Tag)] = [
        arc_spec((0, 0, 50), 5, 0, 2 * math.pi, **xz),  # a full circle
        arc_spec((30, 0, 0), 4, 0, math.pi, **xz),  # a half disc: arc + diameter
        ("line", [26.0, 0.0, 0.0], [34.0, 0.0, 0.0]),
    ]
    ok = run(nx, "nx_measure_section", plane="xz")
    loops = sorted(ok.data["loops"], key=lambda r: r["min"][0])
    assert [r["area"] for r in loops] == pytest.approx([25 * math.pi, 8 * math.pi])
    assert loops[0]["min"] == pytest.approx([-5, 45]) and loops[0]["max"] == pytest.approx([5, 55])
    assert loops[1]["max"] == pytest.approx([34, 4])
    arc = next(s for s in loops[1]["segments"] if s["type"] == "arc")
    assert arc["center"] == pytest.approx([30, 0]) and arc["radius"] == 4
    radial = run(nx, "nx_measure_section", radial_axis="z", at=[50]).data["profile"][0]
    assert flat(radial["spans"]) == pytest.approx([0, 5])  # a solid on the axis starts at 0


def test_open_chains_and_offsets():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    nx.section_cuts[int(body.Tag)] = [
        ("line", [0.0, 3.0, 0.0], [1.0, 3.0, 0.0]),
        ("line", [2.0, 3.0, 1.0], [1.0, 3.0, 0.0]),
    ]
    ok = run(nx, "nx_measure_section", plane="xz", offset=3)
    assert nx.planes[-1][0] == [0.0, 3.0, 0.0]
    (loop,) = ok.data["loops"]
    assert loop["closed"] is False and loop["area"] is None and len(loop["segments"]) == 2
    assert ok.data["area"] == 0.0
    general = run(nx, "nx_measure_section", origin=[1, 2, 3], normal=[1, 0, 0]).data["frame"]
    assert general["u"] == [0.0, 1.0, 0.0] and general["v"] == pytest.approx([0, 0, 1])


def test_an_empty_cut_warns():
    nx = inspection_nx()
    nx.block(10, 10, 10)
    ok = run(nx, "nx_measure_section", plane="xy", offset=99)
    assert ok.ok and ok.data["loops"] == [] and "no loops" in ok.warnings[0]["message"]


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({}, "/plane"),
        ({"plane": "xy", "radial_axis": "z"}, "/radial_axis"),
        ({"normal": [0, 0, 1], "offset": 2}, "/offset"),
        ({"plane": "xy", "at": [1]}, "/at"),
        ({"normal": [0, 0, 0]}, "/normal"),
    ],
)
def test_bad_arguments_are_refused_before_nx(args, field):
    nx = inspection_nx()
    nx.block(10, 10, 10)
    bad = run(nx, "nx_measure_section", **args)
    assert bad.error["field"] == field and bad.mutation_outcome == "not_applicable"
    assert nx.planes == []


def test_cut_choice_errors_say_what_to_send():
    nx = inspection_nx()
    nx.block(10, 10, 10)
    both = run(nx, "nx_measure_section", plane="xz", radial_axis="z", stations=3)
    assert (
        "mutually exclusive" in both.error["message"]
        and "plane and radial_axis" in (both.error["message"])
    )
    assert "radial_axis:'z'" in both.error["hint"]
    none = run(nx, "nx_measure_section")
    assert none.error["field"] == "/plane" and "offset:5" in none.error["hint"]
    stations = run(nx, "nx_measure_section", plane="yz", at=[10, 50])
    assert "radial_axis" in stations.error["message"] and "offset" in stations.error["hint"]


def test_offset_takes_a_unit_string():
    nx = inspection_nx()
    body = nx.block(10, 10, 10)
    nx.section_cuts[int(body.Tag)] = [
        ("line", [0.0, 0.0, 5.0], [10.0, 0.0, 5.0]),
        ("line", [10.0, 0.0, 5.0], [10.0, 10.0, 5.0]),
        ("line", [10.0, 10.0, 5.0], [0.0, 10.0, 5.0]),
        ("line", [0.0, 10.0, 5.0], [0.0, 0.0, 5.0]),
    ]
    ok = run(nx, "nx_measure_section", plane="xy", offset="5 mm")
    assert ok.ok and nx.planes[-1][0] == [0.0, 0.0, 5.0], ok
    assert ok.data["area"] == pytest.approx(100)
    inch = run(nx, "nx_measure_section", plane="xy", offset="0.1 in")
    assert nx.planes[-1][0] == pytest.approx([0.0, 0.0, 2.54]) and inch.ok


def test_an_empty_cut_says_where_the_bodies_are():
    nx = inspection_nx()
    nx.block(10, 10, 10)
    ok = run(nx, "nx_measure_section", plane="xy", offset=99)
    message = ok.warnings[0]["message"]
    assert "the plane is at z = 99" in message and "z 0 to 10" in message, message
    general = run(nx, "nx_measure_section", origin=[0, 0, 50], normal=[0, 0, 1])
    assert "-50 to -40 from the plane" in general.warnings[0]["message"]

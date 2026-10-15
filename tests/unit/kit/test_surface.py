"""kit.surface (shape operators, sampled edge continuity) and the freeform kit additions:
builders.curve_section, curve.point_distance, measure.area, select.curves."""

import math
from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import builders, curve, measure, select, surface
from tests.kit import fake_nx
from tests.unit.freeform.fakes import edge, face, flat, freeform_nx


def test_shape_operator_is_parameterization_invariant():
    plane = surface.shape_operator([1, 0, 0], [0, 1, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0])
    swapped = surface.shape_operator([0, 3, 0], [2, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0])
    assert surface.continuity_difference(plane, swapped) == (0, 0)
    cylinder = surface.shape_operator([0, 2, 0], [0, 0, 1], [-2, 0, 0], [0, 0, 0], [0, 0, 0])
    scaled = surface.shape_operator([0, 4, 0], [0, 0, 3], [-8, 0, 0], [0, 0, 0], [0, 0, 0])
    assert surface.continuity_difference(cylinder, scaled) == pytest.approx((0, 0))
    assert abs(cylinder[1][1][1]) == 0.5
    assert surface.continuity_difference(plane, cylinder)[1] == pytest.approx(0.5)


@pytest.mark.parametrize(
    "derivatives",
    [
        ([0, 0, 0], [1, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]),
        ([1, 0, 0], [2, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]),
    ],
)
def test_singular_parameterisations_are_incomplete_results(derivatives):
    with pytest.raises(NXError) as error:
        surface.shape_operator(*derivatives)
    assert error.value.code == "NX_INCOMPLETE_RESULT"
    assert error.value.details["reason"] == "singular"


def test_crease_angle_is_unsigned():
    up = surface.shape_operator([1, 0, 0], [0, 1, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0])
    down = surface.shape_operator([0, 1, 0], [1, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0])
    tilted = surface.shape_operator([2, 0, 1], [0, 1, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0])
    assert surface.continuity_difference(up, down) == (0, 0)
    assert surface.continuity_difference(up, tilted)[0] == pytest.approx(
        math.degrees(math.atan(0.5))
    )


def test_differential_uses_face_parameters_and_second_derivatives():
    nx = freeform_nx()
    calls = []
    f = face(
        nx, lambda uv: calls.append(uv) or ([1, 0, 0], [0, 1, 0], [0, 0, 0.5], [0] * 3, [0] * 3)
    )
    normal, shape = surface.differential(nx.tx, f, [1, 2, 3])
    assert normal == [0, 0, 1] and shape[0][0] == 0.5 and calls == [[1.0, 2.0, 3.0]]


def test_edge_continuity_samples_gap_angle_and_curvature():
    nx = freeform_nx()
    a_face = face(nx, flat())
    b_face = face(nx, flat(du=(2, 0, 1)))
    a = edge(nx, [0, 0, 0], [0, 10, 0])
    b = edge(nx, [0.001, 10, 0], [0.001, 0, 0])
    report = surface.edge_continuity(nx.tx, a, a_face, b, b_face, samples=4)
    assert report["gap"] == pytest.approx(0.001) and report["samples"] == 4
    assert report["angle"] == pytest.approx(26.56505117707799) and report["curvature"] == 0
    assert report["unresolved"] == 0
    assert report["gap_at"] == {"point": [0, 1.25, 0], "parameter": 0.125}
    assert report["angle_at"]["parameter"] == 0.125 and report["curvature_at"] is not None
    only_gap = surface.edge_continuity(nx.tx, a, None, b, b_face)
    assert only_gap["angle"] is None and only_gap["curvature"] is None
    assert only_gap["angle_at"] is None and only_gap["curvature_at"] is None


def test_edge_continuity_locates_the_worst_samples():
    nx = freeform_nx()
    a_face = face(nx, flat())
    b_face = face(nx, flat(du=(2, 0, 1)))
    a = edge(nx, [0, 0, 0], [0, 10, 0])
    b = edge(nx, [0, 0, 0], [0.4, 10, 0])  # drifts away along a: worst at the last sample
    report = surface.edge_continuity(nx.tx, a, a_face, b, b_face, samples=4)
    assert report["gap_at"]["parameter"] == 0.875
    assert report["gap_at"]["point"] == pytest.approx([0, 8.75, 0])
    assert report["gap"] == pytest.approx(0.35 * 10 / (0.4**2 + 100) ** 0.5)
    singular = face(nx, lambda uv: ([0, 0, 0], [1, 0, 0], [0] * 3, [0] * 3, [0] * 3))
    report = surface.edge_continuity(nx.tx, a, a_face, b, singular, samples=3)
    assert report["unresolved"] == 3 and report["angle"] is None
    with pytest.raises(ValueError):
        surface.edge_continuity(nx.tx, a, a_face, b, b_face, samples=0)


def test_curve_section_mixes_curve_and_edge_rules_with_part_tolerances():
    nx = fake_nx(units="in")
    c = NS(Tag=1, Name="SPLINE")
    e = nx.edge((0, 0, 0), (1, 0, 0))
    section = builders.curve_section(nx.tx, [c, e], start=[1, 2, 3])
    assert section.tolerances == pytest.approx((0.001 / 25.4, 0.001 / 25.4, 0.5))
    assert section.allowed == "only_curves"
    rules, seed, help_point = section.added[0]
    assert rules == [("CurveDumb", [c]), ("EdgeDumb", [e])] and seed is c
    assert (help_point.X, help_point.Y, help_point.Z) == (1.0, 2.0, 3.0)
    with pytest.raises(NXError):
        builders.curve_section(nx.tx, [])


def test_point_distance_and_curves_selection():
    nx = freeform_nx()
    line = edge(nx, [0, 0, 0], [10, 0, 0])
    gap, foot = curve.point_distance(nx.tx, line, [4, 3, 0])
    assert gap == 3 and foot == [4, 0, 0]
    c = nx.curve()
    assert select.curves(nx.tx, [c.ref]) == [c]
    with pytest.raises(NXError) as error:
        select.curves(nx.tx, [c.ref, c.ref], "/boundaries")
    assert error.value.field == "/boundaries/1"


def test_area_is_reported_in_part_units_and_disposed():
    nx = fake_nx(units="in")
    disposed, seen = [], {}

    def props(area_unit, length_unit, accuracy, faces):
        seen.update(units=(area_unit, length_unit), faces=faces)
        return NS(Area=645.16, Dispose=lambda: disposed.append(True))

    nx.part.MeasureManager = NS(NewFaceProperties=props)
    nx.part.UnitCollection = NS(FindObject=lambda name: name)
    f = nx.face()
    assert measure.area(nx.tx, [f]) == pytest.approx(1.0)
    assert seen["units"] == ("SquareMilliMeter", "MilliMeter") and disposed == [True]
    with pytest.raises(NXError):
        measure.area(nx.tx, [])


@pytest.mark.parametrize(
    ("units", "shown", "reading", "want"),
    [("in", "Millimeters", 1.0, 1.0), ("mm", "Inches", 645.16**2, 645.16)],
)
def test_area_of_a_part_beside_a_displayed_part_of_other_units(units, shown, reading, want):
    """A work part that is not displayed: NX 2406 answers in its units but labels them the
    displayed part's (live: an inch sheet of 6 in^2 read 6 mm^2)."""
    nx = fake_nx(units=units)
    nx.part.MeasureManager = NS(NewFaceProperties=lambda *a: NS(Area=reading, Dispose=lambda: None))
    nx.part.UnitCollection = NS(FindObject=lambda name: name)
    unit = getattr(nx.nx.BasePart.Units, shown)
    nx.session.Parts.Display = NS(Tag=987654, PartUnits=unit)
    assert measure.area(nx.tx, [nx.face()]) == pytest.approx(want)
    nx.session.Parts.Display = nx.part  # displayed: NX's reading is the requested units
    assert measure.area(nx.tx, [nx.face()]) == pytest.approx(reading / 645.16 ** (units == "in"))


def test_exact_bounds_project_the_wcs_aligned_box():
    nx = fake_nx()
    # A box aligned to a WCS rotated 90 degrees about z: x along +y, y along -x.
    nx.uf.ModlGeneral = NS(
        AskBoundingBoxExact=lambda tag, csys: ([10, 0, 0], [0, 1, 0, -1, 0, 0, 0, 0, 1], [4, 2, 3]),
        AskBoundingBox=lambda tag: [0, 0, 0, 99, 99, 99],
    )
    body = nx.block(1, 1, 1)
    assert measure.bounds(nx.tx, [body], exact=True) == ([8, 0, 0], [10, 4, 3])
    assert measure.bounds(nx.tx, [body]) == ([0, 0, 0], [99, 99, 99])

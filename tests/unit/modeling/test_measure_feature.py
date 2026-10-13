"""nx_create_measurement on the shared fake, through the real kernel."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx


def measuring_nx(distance=100.5):
    """A modeling fake whose measurement objects make measure features with expressions."""
    nx = modeling_nx()
    calls = []

    def made(kind, values):
        def create():
            f = feature(nx, kind)
            f.expressions = []
            for formula, value, unit in values:
                e = nx.expression(f"p{len(nx.part.Expressions) + 1}", formula, value)
                e.Units = NS(Name=unit, Abbreviation=unit)
                f.expressions.append(e)
            f.parents = list(f.parents)
            return f

        return NS(CreateFeature=create, Dispose=lambda: calls.append(("dispose", kind)))

    mass = nx.part.MeasureManager.NewMassProperties

    def new_mass(units, accuracy, bodies):
        props = mass(units, accuracy, bodies)
        volume = props.Volume
        body = made("BODY_MEASUREMENT", [
            ("body_measurement1.surface_area", 16000.0, "mm^2"),
            ("body_measurement2.volume", volume, "mm^3"),
            ("body_measurement3.mass", 0.5, "kg"),
        ])  # fmt: skip
        props.CreateFeature = body.CreateFeature
        return props

    def new_distance(unit, kind, a, b):
        calls.append(("distance", unit.Name, a, b))
        return made("DISTANCE MEASUREMENT", [("distance8", distance, "mm")])

    def new_length(unit, objects):
        calls.append(("length", unit.Name, list(objects)))
        return made("LENGTH MEASUREMENT", [("length9", sum(o.GetLength() for o in objects), "mm")])

    nx.part.MeasureManager.NewMassProperties = new_mass
    nx.part.MeasureManager.NewDistance = new_distance
    nx.part.MeasureManager.NewLength = new_length
    nx.session.Measurement = NS(GetMinimumDistance=lambda a, b: (100.5, None, None, 0))

    def rename(exp, name):
        exp.Name = name

    nx.part.Expressions.Rename = rename
    nx.calls = calls
    return nx


def test_distance_measurement_names_its_expression_and_checks_the_value():
    nx = measuring_nx()
    a, b = nx.block(10, 10, 10), nx.block(10, 10, 10, origin=(100, 0, 0))
    fa, fb = a.face((1, 0, 0)), b.face((-1, 0, 0))
    ok = run(nx, "nx_create_measurement", kind="distance", first=fa.ref, second=fb.ref,
             name="gap")  # fmt: skip
    assert ok.ok, ok
    assert nx.calls[0] == ("distance", "MilliMeter", fa, fb)
    assert ("dispose", "DISTANCE MEASUREMENT") in nx.calls
    assert ok.data["value"] == pytest.approx(100.5) and ok.data["units"] == "mm"
    assert ok.data["expression"]["expression"]["name"] == "gap"
    assert ok.data["expression"]["quantity"] == "distance"
    assert ok.data["expression"]["formula"] == "distance8"


def test_a_measurement_that_disagrees_with_the_model_rolls_back():
    nx = measuring_nx(distance=99.0)
    a, b = nx.block(10, 10, 10), nx.block(10, 10, 10, origin=(100, 0, 0))
    bad = run(nx, "nx_create_measurement", kind="distance", first=a.ref, second=b.ref)
    assert bad.error["details"]["check"] == "value" and bad.mutation_outcome == "rolled_back"


def test_length_and_body_measurements():
    nx = measuring_nx()
    c1, c2 = nx.curve(), nx.curve()
    c1.GetLength, c2.GetLength = (lambda: 30.0), (lambda: 12.5)
    ok = run(nx, "nx_create_measurement", kind="length", curves=[c1.ref, c2.ref])
    assert ok.ok, ok
    assert nx.calls[0] == ("length", "MilliMeter", [c1, c2])
    assert ok.data["value"] == pytest.approx(42.5) and ok.data["kind"] == "length"
    body = nx.block(80, 80, 10)
    vol = run(nx, "nx_create_measurement", kind="body", bodies=[body.ref], name="hull_volume")
    assert vol.ok, vol
    assert vol.data["value"] == pytest.approx(64000)
    assert [r["quantity"] for r in vol.data["expressions"]] == ["surface_area", "volume", "mass"]
    assert vol.data["expression"]["expression"]["name"] == "hull_volume"


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"kind": "distance", "first": "FA"}, "/second"),
        ({"kind": "length"}, "/curves"),
        ({"kind": "length", "curves": ["C"], "bodies": ["B"]}, "/bodies"),
        ({"kind": "distance", "first": "FA", "second": "FA"}, "/second"),
    ],
)
def test_arguments_by_kind_are_checked_first(extra, field):
    nx = measuring_nx()
    body = nx.block(10, 10, 10)
    c = nx.curve()
    names = {"FA": body.face((1, 0, 0)).ref, "C": c.ref, "B": body.ref}
    args = {k: ([names[x] for x in v] if isinstance(v, list) else names.get(v, v))
            for k, v in extra.items()}  # fmt: skip
    bad = run(nx, "nx_create_measurement", **args)
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started"


def test_a_taken_name_is_refused_before_measuring():
    nx = measuring_nx()
    nx.expression("gap", "5", 5.0)
    body = nx.block(10, 10, 10)
    bad = run(nx, "nx_create_measurement", kind="body", bodies=[body.ref], name="gap")
    assert bad.error["code"] == "NX_NAME_CONFLICT" and bad.mutation_outcome == "not_started"
    assert not nx.calls


def test_a_sheet_body_is_refused():
    nx = measuring_nx()
    body = nx.block(10, 10, 10)
    body.IsSolidBody = False
    bad = run(nx, "nx_create_measurement", kind="body", bodies=[body.ref])
    assert bad.error["field"] == "/bodies/0" and bad.error["code"] == "NX_WRONG_GEOMETRY"

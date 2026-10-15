"""Dimension lines of sets and ordinates placed by margin (the gap from the view border),
off-sheet refusals, overlap warnings, and linear dimensions measured to circle centres."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.unit.drawing.test_dimension_kinds import create, kinds_nx, rebind_nx, with_sets


def ordinates(nx, values):
    """UF ordinate calls: dimensions measuring ``values`` in turn, on the new origin."""
    made = {}
    placed = []

    def origin(*_):
        tag = 8000 + len(made)
        made[tag] = NS(Tag=tag, Name="ORIGIN", OwningPart=nx.part, IsOccurrence=False)
        return tag

    def dimension(margin, kind, data, *rest):
        tag = 8100 + len(placed)
        placed.append(rest[-1])
        d = NS(Tag=tag, Name="ORD", ComputedSize=values[len(placed) - 1], OwningPart=nx.part,
               IsOccurrence=False, GetOrdinateOrigin=lambda: made[8000])  # fmt: skip
        made[tag] = d
        nx.part.Dimensions.append(d)
        return tag

    nx.uf.Drf = NS(CreateOrdorigin=origin, CreateOrdinateMargin=lambda *a: 8050,
                   CreateOrddimension=dimension)  # fmt: skip
    nx.nx.TaggedObjectManager = NS(GetTaggedObject=lambda tag: made[int(tag)])
    return placed


def test_an_ordinate_margin_is_the_gap_above_the_view_border():
    nx = kinds_nx()
    placed = ordinates(nx, [25.0, 80.0])
    ok = create(nx, kind="ordinate", datum=nx.left.ref, objects=[nx.hole.ref, nx.right.ref],
                margin=40)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["margin"] == 40 and ok.data["margin_line"] == pytest.approx(145)  # 105 + 40
    assert [p[1] for p in placed] == pytest.approx([145, 145])
    nx = kinds_nx()
    ordinates(nx, [21.0])  # the hole's y from the left edge's start
    default = create(nx, kind="ordinate", datum=nx.left.ref, objects=[nx.hole.ref],
                     direction="vertical")  # fmt: skip
    assert default.data["margin"] == 15 and default.data["margin_line"] == pytest.approx(75)


def test_a_margin_off_the_sheet_or_on_other_kinds_is_refused_before_nx():
    nx = kinds_nx()
    ordinates(nx, [25.0])
    off = create(nx, kind="ordinate", datum=nx.left.ref, objects=[nx.hole.ref], margin=500)
    assert off.error["field"] == "/margin" and off.mutation_outcome == "not_started"
    assert "off the sheet" in off.error["message"]
    negative = create(nx, kind="ordinate", datum=nx.left.ref, objects=[nx.hole.ref], margin=-5)
    assert negative.error["field"] == "/margin" and negative.mutation_outcome == "not_started"
    linear = create(nx, kind="linear", first=nx.back.ref, margin=10)
    assert linear.error["field"] == "/margin"
    with_sets(nx)
    both = create(nx, kind="chain", objects=[nx.left.ref, nx.hole.ref, nx.right.ref],
                  margin=10, origin=[90, 160])  # fmt: skip
    assert both.error["field"] == "/margin" and both.mutation_outcome == "not_started"
    away = create(nx, kind="chain", objects=[nx.left.ref, nx.hole.ref, nx.right.ref],
                  origin=[90, 900])  # fmt: skip
    assert away.error["field"] == "/origin" and away.mutation_outcome == "not_started"


def test_a_set_margin_places_its_line_and_default_sets_step_past_earlier_ones():
    nx = kinds_nx()
    sets = with_sets(nx)
    objects = [nx.left.ref, nx.hole.ref, nx.right.ref]
    far = create(nx, kind="baseline", objects=objects, margin=40)
    assert far.ok, far
    assert far.data["origin"] == pytest.approx([100, 145]) and far.data["margin"] == 40
    earlier = NS(Tag=6000, AnnotationOrigin=NS(X=100, Y=117, Z=0))  # a chain on the default line
    nx.make_sheet  # noqa: B018 - the sheet the view is on
    sheet = nx.part.DrawingSheets[0]
    sheet.View.AskVisibleObjects = lambda: [earlier]
    nx.uf.ModlGeneral = NS(AskBoundingBox=lambda tag: [90.0, 114.0, 0.0, 190.0, 120.0, 0.0]
                           if int(tag) == 6000 else [0.0] * 6)  # fmt: skip
    stepped = create(nx, kind="chain", objects=objects)
    assert stepped.ok, stepped
    assert stepped.data["origin"] == pytest.approx([100, 129])  # 105 + 12, then one step out
    assert pytest.approx(129) == sets.made[-1].origin.Y


def test_a_linear_dimension_to_a_circle_measures_to_its_centre():
    nx, d, ref = rebind_nx()
    nx.size = 25.0  # NX measures from the line to the hole's centre
    ok = run(nx, "nx_edit_dimension", dimension=ref, edges=[nx.left.ref, nx.hole.ref])
    assert ok.ok, ok
    builder = nx.last("LinearDimensionBuilder")
    snap = builder.SecondAssociativity.SetValue.calls[-1][0][0]
    assert snap is nx.nx.InferSnapType.SnapType.Center
    point = builder.SecondAssociativity.SetValue.calls[-1][0][3]
    assert pytest.approx((25, 25)) == (point.X, point.Y)

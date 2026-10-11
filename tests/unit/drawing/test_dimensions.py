"""Drawing dimensions: pure geometry rules, refusals before NX and value read-back."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.domains.drawing import dimensions as dims
from tests.kit import run
from tests.unit.drawing.fakes import arc_edge, drawing_nx, install_eval, line_edge, make_view


def test_angles_match_the_acute_angle_its_supplement_and_reflex_forms():
    assert dims.acute_angle_deg([1, 0], [1, 1]) == pytest.approx(45)
    assert dims.acute_angle_deg([1, 0], [-1, 1]) == pytest.approx(45)
    assert dims.acute_angle_deg([0, 0], [1, 0]) is None
    for value in (45, 135, 225, 315, 405):
        assert dims.angle_matches(value, 45)
    assert not dims.angle_matches(60, 45)


def test_linear_values_follow_the_view_axes():
    axes = ([1, 0, 0], [0, 1, 0], [0, 0, 1])
    a, b = [0, 0, 0], [30, 40, 7]
    assert dims.linear_value(a, b, axes, "horizontal") == 30
    assert dims.linear_value(a, b, axes, "vertical") == 40
    assert dims.linear_value(a, b, axes, "aligned") == 50
    side = ([0, 1, 0], [0, 0, 1], [1, 0, 0])
    assert dims.linear_value(a, b, side, "horizontal") == 40


def test_default_text_leaves_the_view_on_the_span_side():
    box = [0.0, 0.0, 100.0, 50.0]
    assert dims.linear_direction([0, 40], [10, 40], "horizontal", box) == ([5, 40], [0, 1])
    assert dims.linear_direction([0, 5], [10, 5], "horizontal", box) == ([5, 5], [0, -1])
    assert dims.linear_direction([90, 0], [90, 10], "vertical", box) == ([90, 5], [1, 0])
    assert dims.linear_direction([10, 0], [10, 10], "vertical", box) == ([10, 5], [-1, 0])
    mid, normal = dims.linear_direction([0, 40], [10, 50], "aligned", box)  # upper left
    assert normal == pytest.approx([-(0.5**0.5), 0.5**0.5])


def test_ordinate_snaps():
    arc = {"kind": "arc", "closed": True}
    assert dims.ordinate_snap(arc, "auto", "/o") == "center"
    assert dims.ordinate_snap({"kind": "line"}, "auto", "/o") == "start"
    assert dims.ordinate_snap({"kind": "arc", "closed": False}, "end", "/o") == "end"
    for info, snap in [({"kind": "line"}, "center"), (arc, "start"), (arc, "end")]:
        with pytest.raises(NXError) as error:
            dims.ordinate_snap(info, snap, "/objects/0")
        assert error.value.code == "NX_WRONG_GEOMETRY" and error.value.field == "/objects/0"


def dims_nx():
    nx = drawing_nx()
    sheet = nx.make_sheet()
    nx.top = make_view(nx, sheet, pos=(100, 100))
    nx.side = make_view(
        nx, sheet, "Front@2", pos=(100, 40), axes=((1, 0, 0), (0, 0, 1), (0, -1, 0))
    )
    nx.back = line_edge(nx, (0, 50, 20), (70, 50, 20))
    nx.front = line_edge(nx, (80, 0, 20), (0, 0, 20))
    nx.hole = arc_edge(nx, (25, 25, 20), 8)
    install_eval(nx, [nx.back, nx.front, nx.hole])
    return nx


def dimension_hook(nx, value):
    def commit(b):
        p = b.Origin.OriginPoint
        d = NS(Tag=7000 + len(nx.part.Dimensions), Name="DIM", ComputedSize=value,
               AnnotationOrigin=p, OwningPart=nx.part, IsOccurrence=False)  # fmt: skip
        nx.part.Dimensions.append(d)
        return d

    return commit


def test_linear_dimension_value_is_checked_against_the_model():
    nx = dims_nx()
    nx.on_commit("LinearDimensionBuilder", dimension_hook(nx, 70.0))
    ok = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="linear",
             first=nx.back.ref, orientation="horizontal")  # fmt: skip
    assert ok.ok, ok
    assert ok.data["dimension"]["value"] == 70 and ok.data["dimension"]["expected"] == 70
    assert ok.data["origin"] == pytest.approx([135.0, 160.0])  # 10 mm above the edge
    assert nx.live_builders == 0
    nx.on_commit("LinearDimensionBuilder", dimension_hook(nx, 71.0))
    bad = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="linear",
              first=nx.back.ref, orientation="horizontal")  # fmt: skip
    assert bad.error["category"] == "verification_failed"
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0


def test_radial_refusals_happen_before_nx():
    nx = dims_nx()
    edge_on = run(
        nx, "nx_create_drawing_dimension", view=nx.side.ref, kind="radius", first=nx.hole.ref
    )
    assert edge_on.error["code"] == "NX_UNSUPPORTED_GEOMETRY" and edge_on.error["field"] == "/first"
    straight = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="diameter",
                   first=nx.back.ref)  # fmt: skip
    assert straight.error["code"] == "NX_WRONG_GEOMETRY"
    two = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="radius", first=nx.hole.ref,
              second=nx.back.ref)  # fmt: skip
    assert two.error["field"] == "/second"
    for result in (edge_on, straight, two):
        assert result.mutation_outcome == "not_started"
    assert not nx.builders


def test_angle_and_ordinate_refusals():
    nx = dims_nx()
    parallel = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="angle",
                   first=nx.back.ref, second=nx.front.ref)  # fmt: skip
    assert parallel.error["field"] == "/second" and parallel.error["category"] == "invalid_argument"
    same = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="angle",
               first=nx.back.ref, second=nx.back.ref)  # fmt: skip
    assert same.error["field"] == "/second"
    dup = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="ordinate",
              datum=nx.front.ref, objects=[nx.hole.ref, nx.hole.ref])  # fmt: skip
    assert dup.error["field"] == "/objects/1"
    self_ref = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="ordinate",
                   datum=nx.front.ref, objects=[nx.front.ref])  # fmt: skip
    assert self_ref.error["field"] == "/objects"
    circle_end = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="ordinate",
                     datum=nx.front.ref, objects=[nx.hole.ref], object_point="start")  # fmt: skip
    assert circle_end.error["code"] == "NX_WRONG_GEOMETRY"
    assert circle_end.error["field"] == "/objects/0"


def test_edit_dimension_validation():
    nx = dims_nx()
    d = NS(Tag=8001, Name="R", ComputedSize=8.0, NumberOfAssociativities=1, IsRetained=False,
           OwningPart=nx.part, IsOccurrence=False)  # fmt: skip
    nx.part.Dimensions.append(d)
    ref = nx.ref_of(d, "dimension")
    nothing = run(nx, "nx_edit_dimension", dimension=ref)
    assert nothing.error["category"] == "invalid_argument"
    decimals = run(nx, "nx_edit_dimension", dimension=ref, format={"decimals": 9})
    assert decimals.error["field"] == "/format/decimals"
    units = run(nx, "nx_edit_dimension", dimension=ref, format={"units": "ft"})
    assert units.error["field"] == "/format/units"
    d.GetAssociativity = lambda i: NS(ObjectView=nx.top)
    radial = run(nx, "nx_edit_dimension", dimension=ref, edges=[nx.back.ref])
    assert radial.error["code"] == "NX_UNSUPPORTED_OPTION" and radial.error["field"] == "/edges"
    for result in (nothing, decimals, units, radial):
        assert result.mutation_outcome == "not_started"


def test_the_view_may_be_given_by_name():
    nx = dims_nx()
    nx.on_commit("LinearDimensionBuilder", dimension_hook(nx, 70.0))
    for name in ("TOP@1", "top"):
        ok = run(nx, "nx_create_drawing_dimension", view=name, kind="linear", first=nx.back.ref,
                 orientation="horizontal")  # fmt: skip
        assert ok.ok, (name, ok)
        assert ok.data["origin"] == pytest.approx([135.0, 160.0])
    missing = run(nx, "nx_create_drawing_dimension", view="Side@9", kind="linear",
                  first=nx.back.ref)  # fmt: skip
    assert missing.error["code"] == "NX_NOT_FOUND" and missing.error["field"] == "/view"
    assert len(missing.error["details"]["views"]) == 2 and "v:N" in missing.error["hint"]
    make_view(nx, nx.part.DrawingSheets[0], "Top@3")
    twice = run(nx, "nx_create_drawing_dimension", view="top", kind="linear", first=nx.back.ref)
    assert (
        twice.error["code"] == "NX_INVALID_ARGUMENT" and "More than one" in twice.error["message"]
    )
    for result in (missing, twice):
        assert result.mutation_outcome == "not_started"

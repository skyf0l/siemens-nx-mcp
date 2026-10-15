"""Hole callouts and hole tables: callout text from NX parameters, hole-table rows,
position matching and refusals before NX."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.drawing import hole_callouts as holes
from nx_mcp.domains.drawing.common import table_kind, table_tag
from tests.kit import run
from tests.unit.drawing.fakes import arc_edge, drawing_nx, install_eval, line_edge, make_view

D = holes.DIAMETER


def test_callout_text_follows_the_parameters_nx_shows():
    params = [
        ("PatternFeatureCount", True, [""], "", [" X"]),
        ("Diameter", True, ["<o> "], "6.0", [""]),
        ("Depth", True, [" - "], "12.0", [" DEEP"]),
        ("LineBreak", True, [], "", []),
        ("CounterBoreDiameter", True, ["CBORE<o> "], "11.0", [""]),
        ("CounterBoreDepth", True, [" - "], "4.0", [" DEEP"]),
        ("TaperAngle", False, ["TAPER "], "3.0", [""]),
    ]
    assert holes.compose(params) == [f"{D} 6 - 12 DEEP", f"CBORE{D} 11 - 4 DEEP"]
    assert holes.compose(params, count=4)[0] == f"4X {D} 6 - 12 DEEP"
    assert holes.compose(params, count=1)[0] == f"{D} 6 - 12 DEEP"
    assert holes.values(params) == {
        "diameter": 6.0,
        "depth": 12.0,
        "counterbore_diameter": 11.0,
        "counterbore_depth": 4.0,
    }


def test_thread_values_keep_their_text():
    params = [
        ("ThreadSize", True, [""], '"M8 x 1.25"', [""]),
        ("ThreadTolerance", True, ["-"], "6H", [""]),
        ("Pitch", False, [""], "1.25 // Used By ...", [""]),
        ("Depth", True, [" - "], "15.0", [" DEEP"]),
    ]
    assert holes.compose(params) == ["M8 x 1.25-6H - 15 DEEP"]
    assert holes.values(params) == {"thread_size": "M8 x 1.25", "thread_tolerance": "6H",
                                    "depth": 15.0}  # fmt: skip


def test_value_cleaning_and_numbers():
    assert holes.clean_value("6.0") == "6"
    assert holes.clean_value("6.80") == "6.8"
    assert holes.clean_value("1.25 // Used By p12") == "1.25"
    assert holes.to_number("6,8") == pytest.approx(6.8)
    assert holes.to_number("-") is None
    assert holes.snake("CounterSinkAngle") == "counter_sink_angle"


ROWS = [
    ["Hole Table: Top@1", "", "", ""],
    ["Hole No.", "X", "Y", "Z"],
    ["Part : block - Body : 1", "", "", ""],
    ["Blind Hole <O>6.00 T10.00", "", "", ""],
    ["2", "70.00", "40.00", "-"],
    ["1", "10.00", "40.00", "-"],
    ["Counterbore <O>6.00 T12.00 / <O>11.00 T4.00", "", "", ""],
    ["5", "25,00", "25.00", "-4.00"],
    ["", "", "", "-"],
]


def test_hole_table_rows_are_parsed_and_sorted_by_position():
    found = holes.parse_hole_table(ROWS)
    assert [h["label"] for h in found] == ["1", "5", "2"]
    assert found[0] == {
        "label": "1",
        "position": [10.0, 40.0],
        "size": f"Blind Hole {D}6.00 T10.00",
    }
    assert found[1]["position"] == [25.0, 25.0] and found[1]["z"] == -4.0
    assert found[1]["size"].startswith("Counterbore")
    assert holes.parse_hole_table([["title", "", ""]]) == []
    assert holes.places(ROWS) == 2


def test_positions_match_per_axis_within_the_shown_rounding():
    expected = [[0.3937, 1.5748], [2.1654, 0.9843]]
    shown = [[2.17, 0.98], [0.39, 1.57]]
    assert holes.match_positions(expected, shown, 0.0051) == []
    assert holes.match_positions(expected, shown[:1], 0.0051) == [0]
    assert holes.match_positions([[0, 0], [0, 0]], [[0, 0]], 0.01) == [1]


def test_hole_tables_are_a_table_kind():
    nx = drawing_nx()
    table = type("HoleTable", (), {})()
    table.Tag = 4242
    tx = NS(part=nx.part, uf=nx.uf)
    assert table_kind(tx, table) == "hole"
    assert table_tag(tx, table) == 4242


def holes_nx():
    nx = drawing_nx()
    sheet = nx.make_sheet()
    nx.top = make_view(nx, sheet, pos=(100, 100))
    nx.corner = line_edge(nx, (0, 0, 20), (0, 0, 0))
    nx.hole = arc_edge(nx, (25, 25, 20), 3)
    install_eval(nx, [nx.corner, nx.hole])
    nx.part.Features = []
    return nx


def test_callout_refusals_happen_before_nx():
    nx = holes_nx()
    neither = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="hole_callout")
    assert neither.error["code"] == "NX_INVALID_ARGUMENT" and neither.error["field"] == "/hole"
    assert "exactly one" in neither.error["message"]
    both = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="hole_callout",
               hole="f:1", first=nx.hole.ref)  # fmt: skip
    assert both.error["field"] == "/first" and "not both" in both.error["message"]
    second = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="hole_callout",
                 first=nx.hole.ref, second=nx.corner.ref)  # fmt: skip
    assert second.error["field"] == "/second"
    not_hole = run(nx, "nx_create_drawing_dimension", view=nx.top.ref, kind="hole_callout",
                   first=nx.corner.ref)  # fmt: skip
    assert not_hole.error["code"] == "NX_WRONG_KIND" and not_hole.error["field"] == "/first"
    for result in (neither, both, second, not_hole):
        assert result.mutation_outcome == "not_started"
    assert not nx.builders


def test_hole_table_needs_a_view_and_an_origin():
    nx = holes_nx()
    no_origin = run(nx, "nx_create_drawing_table", kind="hole", view=nx.top.ref,
                    position=[30, 120])  # fmt: skip
    assert no_origin.error["code"] == "NX_INVALID_ARGUMENT"
    no_view = run(nx, "nx_create_drawing_table", kind="hole", position=[30, 120],
                  origin=nx.corner.ref)  # fmt: skip
    assert no_view.error["code"] == "NX_INVALID_ARGUMENT"
    for result in (no_origin, no_view):
        assert result.mutation_outcome == "not_started"
    assert not nx.builders


class _Feature:
    def __init__(self, tag, edges, kind, origins=()):
        self.Tag, self._edges, self.FeatureType, self._origins = tag, edges, kind, origins

    def GetEdges(self):
        return self._edges

    def GetOrigins(self):
        return list(self._origins)


def test_callout_count_covers_only_the_selected_holes():
    edges = [NS(Tag=t) for t in (11, 12, 21, 22)]
    package = type("HolePackage", (_Feature,), {})(1, edges, "HOLE PACKAGE", origins=range(4))
    first = _Feature(2, edges[:2], "SIMPLE HOLE")
    tx = NS(part=NS(Features=[package, first]))
    whole, pkg = holes.hole_of_feature(tx, package, "/hole")
    assert holes.shown_count(pkg, whole=whole is pkg) == 4  # the package: 4X
    one, pkg = holes.hole_of_feature(tx, first, "/hole")
    assert one is first and holes.shown_count(pkg, whole=one is pkg) == 1  # one instance
    target, pkg = holes.hole_of_edge(tx, edges[0], "/first")
    assert target is first and holes.shown_count(pkg, whole=False) == 1  # one edge

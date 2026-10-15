"""Kit additions used by the drawing domain: sheet geometry, view placement, annotation
owners, edge facts, interior face points and field-aware reference resolution."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import drawing, facts, select
from tests.kit import fake_nx
from tests.unit.drawing.fakes import arc_edge, drawing_nx, install_eval, line_edge


def test_defaults_are_millimetres_in_sheet_units():
    assert drawing.from_mm("mm", 100, 60) == [100.0, 60.0]
    assert drawing.from_mm("in", 100) == pytest.approx([100 / 25.4])


def test_place_moves_and_verifies_only_the_free_axis():
    nx = drawing_nx()
    sheet = nx.make_sheet()
    view = nx.make_view(sheet, pos=(50, 50))
    assert drawing.place(nx.tx, view, [120, 80]) == [120.0, 80.0]
    assert nx.part.DraftingViews.updated[-1] == [view]
    view.locked = 1  # aligned: NX keeps y
    with pytest.raises(NXError) as error:
        drawing.place(nx.tx, view, [130, 90])
    assert error.value.code == "NX_VERIFICATION_FAILED"
    assert error.value.details["check"] == "position"
    assert drawing.place(nx.tx, view, [140, 90], axes=(0,)) == [140.0, 80.0]


def test_view_geometry_queries():
    nx = drawing_nx()
    sheet = nx.make_sheet(size=(297.0, 210.0))
    view = nx.make_view(sheet, pos=(100, 90), scale=2)
    assert drawing.position(view) == [100.0, 90.0]
    assert drawing.scale(nx.tx, view) == 2.0
    assert drawing.borders(nx.tx, view) == [80.0, 80.0, 120.0, 100.0]
    assert drawing.inside(drawing.borders(nx.tx, view), sheet)
    assert not drawing.inside([-1, 0, 10, 10], sheet)
    assert drawing.to_sheet(nx.tx, view, [5, 1, 9]) == [110.0, 92.0]
    assert drawing.axes(view)[2] == [0, 0, 1]
    assert drawing.size(sheet) == [297.0, 210.0]


def test_annotation_sheet_from_view_dependency_and_table_sections():
    nx = drawing_nx()
    first, second = nx.make_sheet("A"), nx.make_sheet("B")
    note = nx._own(NS(Tag=901, AnnotationOrigin=None), "annotation")
    nx.dependent[901] = (1, "B@0")
    assert drawing.sheet_of_annotation(nx.tx, note) is second
    nx.dependent[901] = (0, "")
    assert drawing.sheet_of_annotation(nx.tx, note) is None  # PMI: no sheet
    nx.dependent[901] = (1, "Top@1")  # a member view is not a sheet
    assert drawing.sheet_of_annotation(nx.tx, note) is None
    # A bend table has no origin: its first table section carries it.
    section = NS(Tag=902, AnnotationOrigin="origin")
    table = NS(Tag=903)
    nx.uf.Tabnot = NS(AskNmSections=lambda tag: 1, AskNthSection=lambda tag, i: 902)
    nx.nx.TaggedObjectManager = NS(GetTaggedObject=lambda tag: section)
    nx.dependent[902] = (1, "A@0")
    assert drawing.anchor_of(nx.tx, table) is section
    assert drawing.anchor_of(nx.tx, note) is note
    assert drawing.sheet_of_annotation(nx.tx, table) is first


def test_in_modeling_closes_and_reopens_the_shown_sheet():
    nx = fake_nx()
    sheet = nx.sheet()
    sheet.Open()
    with drawing.in_modeling(nx.tx):
        assert drawing.current(nx.part) is None
    assert drawing.current(nx.part) is sheet
    with drawing.in_modeling(nx.tx):  # nothing shown: nothing reopened
        pass
    drawing.close_sheets(nx.tx)
    with drawing.in_modeling(nx.tx):
        pass
    assert drawing.current(nx.part) is None


def test_edge_facts_lines_full_circles_and_partial_arcs():
    nx = drawing_nx()
    line = line_edge(nx, (0, 0, 0), (10, 0, 0))
    hole = arc_edge(nx, (25, 25, 20), 8)
    quarter = arc_edge(nx, (0, 0, 0), 5, full=False)
    side = arc_edge(nx, (0, 0, 0), 5, normal=(0, 1, 0))
    install_eval(nx, [line, hole, quarter, side])
    f = facts.edge(nx.tx, line)
    assert f["kind"] == "line" and f["start"] == [0, 0, 0] and f["end"] == [10, 0, 0]
    f = facts.edge(nx.tx, hole)
    assert f["kind"] == "arc" and f["radius"] == 8 and f["closed"]
    assert f["center"] == [25, 25, 20] and f["normal"] == pytest.approx([0, 0, 1])
    assert not facts.edge(nx.tx, quarter)["closed"]
    assert drawing.seen_true_shape(facts.edge(nx.tx, side)["normal"], [0, 0, 1]) is False
    assert facts.on_object(nx.tx, line) == pytest.approx([5, 0, 0])


def test_face_point_prefers_points_clear_of_holes():
    nx = fake_nx()
    face = nx.face()

    def props(tag, uv):
        return ([uv[0], uv[1], 0.0],)

    def inside(p, tag):  # a hole in the middle of a 0..1 x 0..1 face
        return 2 if abs(p[0] - 0.5) < 0.2 and abs(p[1] - 0.5) < 0.2 else 1

    nx.uf.Modeling = NS(
        AskFaceUvMinmax=lambda tag: (0.0, 1.0, 0.0, 1.0),
        AskFaceProps=props,
        AskPointContainment=inside,
    )
    p = facts.face_point(nx.tx, face)
    assert inside(p, face.Tag) == 1
    assert facts.on_object(nx.tx, face) == p


def test_resolve_names_the_field_of_a_bad_reference():
    nx = fake_nx()
    body = nx.block(1, 1, 1)
    with pytest.raises(NXError) as error:
        select.resolve(nx.tx, body.ref, "edge", field="/first")
    assert error.value.code == "NX_WRONG_KIND" and error.value.field == "/first"
    with pytest.raises(NXError) as error:
        select.resolve(nx.tx, "e:99999", "edge", field="/second")
    assert error.value.field == "/second"
    assert select.resolve(nx.tx, body.ref, "body") is body


def test_text_spots_clear_the_view_border_and_other_annotations():
    box = [100.0, 50.0, 200.0, 150.0]
    assert drawing.clear_of(box, [150, 140], [0, 1], 10) == [150.0, 160.0]
    assert drawing.clear_of(box, [150, 60], [0, -1], 10) == [150.0, 40.0]
    assert drawing.clear_of(box, [150, 100], [1, 0], 5) == [205.0, 100.0]
    diagonal = drawing.clear_of(box, [150, 100], [0.6, 0.8], 10)  # leaves through the top
    assert diagonal == pytest.approx([150 + 62.5 * 0.6 + 6, 150 + 8])
    assert drawing.clear_of(box, [250, 100], [1, 0], 10) == [260.0, 100.0]  # already outside
    assert drawing.outward(box, [180, 100]) == [1.0, 0.0]
    assert drawing.outward(box, [150, 100]) == pytest.approx([0.5**0.5, 0.5**0.5])
    taken = [[140.0, 155.0, 170.0, 165.0]]
    assert drawing.free_spot([150, 160], [0, 1], 10, taken) == [150.0, 170.0]
    assert drawing.free_spot([150, 190], [0, 1], 10, taken) == [150.0, 190.0]
    assert drawing.overlap(box, [190, 140, 300, 300]) and not drawing.overlap(box, [200, 0, 1, 1])


def test_a_view_of_another_part_names_both_parts():
    nx = drawing_nx()
    sheet = nx.make_sheet()
    view = nx.make_view(sheet)
    nx.part.FullPath = "C:/ws/asm.prt"
    sheet.views.remove(view)
    view.OwningPart = NS(FullPath="C:/ws/plate.prt", Name="plate")
    with pytest.raises(NXError) as error:
        drawing.view_sheet(nx.tx, view, "/view")
    assert error.value.code == "NX_WRONG_OWNER" and error.value.field == "/view"
    assert "plate.prt" in error.value.message and "asm.prt" in error.value.message
    view.OwningPart = nx.part  # in the part but on no sheet
    with pytest.raises(NXError) as error:
        drawing.view_sheet(nx.tx, view, "/view")
    assert "one sheet" in error.value.message

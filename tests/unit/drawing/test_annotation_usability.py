"""Natural arguments and clear results of GD&T, symbols, center marks and balloons."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.drawing import annotations
from tests.kit import run
from tests.kit.fake import Node
from tests.kit.objects import Object, point
from tests.unit.drawing.fakes import arc_edge, install_eval, line_edge, make_view
from tests.unit.drawing.test_annotations import notes_nx
from tests.unit.drawing.test_gdt_views import gdt_nx, view_args

# ---- GD&T -----------------------------------------------------------------------------------


def test_gdt_takes_any_case_letters_unit_tolerances_and_a_default_origin():
    nx = gdt_nx()
    datum = run(nx, "nx_create_gdt", kind="view_datum_feature", letter="a", view=nx.top.ref,
                object=nx.front.ref)  # fmt: skip
    assert datum.ok, datum
    assert datum.data["letter"] == "A"
    anchor = datum.data["leader"]["point"]
    assert datum.data["origin"] == pytest.approx([anchor[0] + 20, anchor[1] + 20])
    frame = {"characteristic": "flatness", "tolerance": "0.05 mm"}
    ok = run(nx, "nx_create_gdt", kind="view_fcf", frame=frame,
             **view_args(nx, nx.hole, [150, 120]))  # fmt: skip
    assert ok.ok, ok
    assert ok.data["frame"]["tolerance"] == pytest.approx(0.05)


def test_gdt_default_origin_follows_the_sheet_units():
    nx = gdt_nx(sheet_units="in")
    ok = run(nx, "nx_create_gdt", kind="view_datum_feature", letter="B", view=nx.top.ref,
             object=nx.front.ref)  # fmt: skip
    assert ok.ok, ok
    anchor = ok.data["leader"]["point"]
    step = 20 / 25.4
    assert ok.data["origin"] == pytest.approx([anchor[0] + step, anchor[1] + step])


def test_gdt_names_the_field_its_kind_needs_with_an_example():
    nx = gdt_nx()
    bad = run(nx, "nx_create_gdt", kind="view_fcf", view=nx.top.ref, object=nx.front.ref)
    assert bad.error["field"] == "/frame" and "required for kind view_fcf" in bad.error["message"]
    assert '"characteristic": "flatness"' in bad.error["hint"]
    zero = run(nx, "nx_create_gdt", kind="view_fcf", view=nx.top.ref, object=nx.front.ref,
               frame={"characteristic": "flatness", "tolerance": "0 mm"})  # fmt: skip
    assert zero.error["field"] == "/frame/tolerance"
    for result in (bad, zero):
        assert result.mutation_outcome == "not_started"


def pmi_nx():
    """gdt_nx with PMI feature control frames: reopened builders read the stored frame."""
    nx = gdt_nx()

    def factory(existing):
        b = nx.builder("PmiFeatureControlFrameBuilder", existing)
        if existing is None:
            row = Node("row")
            b.FeatureControlFrameDataList = NS(FindItem=lambda i: row)
            return b
        src = nx.made[int(existing.Tag)]
        b.Characteristic = src.Characteristic
        b.FeatureControlFrameDataList = src.FeatureControlFrameDataList
        return b

    def commit(b):
        obj = type("Fcf", (Object,), {})("FCF")
        at = b.Origin.Origin.SetValue.calls[-1][0][2]
        obj.AnnotationOrigin = point(at.X, at.Y, 0.0)  # NX moves it onto its plane
        obj.GetText = lambda: ["<&70><+><&2>"]
        obj.OwningPart, obj.IsOccurrence = nx.part, False
        nx.made[int(obj.Tag)] = b
        return obj

    nx.on_commit("PmiFeatureControlFrameBuilder", commit)
    nx.part.Annotations.CreatePmiFeatureControlFrameBuilder = factory
    return nx


def test_a_pmi_frame_is_read_back_in_words_and_a_moved_position_is_named():
    nx = pmi_nx()
    face = nx.face()
    ok = run(nx, "nx_create_gdt", kind="fcf", faces=[face.ref], position=[50, 30, 10],
             frame={"characteristic": "flatness", "tolerance": "0.05 mm"})  # fmt: skip
    assert ok.ok, ok
    assert ok.data["frame"] == {
        "characteristic": "flatness", "tolerance": 0.05, "material": "none", "zone": "none",
        "datums": [], "datum_material": [],
    }  # fmt: skip
    assert ok.data["position"] == pytest.approx([50, 30, 0])
    assert any("[50.0, 30.0, 0.0]" in w["message"] for w in ok.warnings), ok.warnings
    assert nx.live_builders == 0


# ---- symbols ----------------------------------------------------------------------------------


def symbols_nx():
    nx = notes_nx()
    made = {}
    symbols = nx.nx.Annotations.LineWeldDataBuilder.WeldSymbolType

    def factory(kind):
        def make(existing):
            b = nx.builder(kind, existing)
            if not isinstance(existing, Object):  # a new symbol: NX passes Null
                for side in ("ArrowSideWeldData", "OtherSideWeldData"):
                    setattr(b, side, NS(WeldSymbol=getattr(symbols, "None"), WeldSize=""))
                return b
            src = made[int(existing.Tag)]
            for name in ("Finish", "A2", "ArrowSideWeldData", "OtherSideWeldData"):
                setattr(b, name, getattr(src, name))
            data = src.Leader.Leaders.Append.calls[-1][0][0]
            attached = data.Leader.SetValue.calls[-1][0]
            b.Leader = NS(Leaders=NS(Length=1, FindItem=lambda i: NS(
                Leader=NS(GetValue=lambda: attached))))  # fmt: skip
            return b

        return make

    def commit(b):
        obj = Object("SYMBOL")
        obj.AnnotationOrigin = b.Origin.Origin.SetValue.calls[-1][0][2]
        obj.OwningPart, obj.IsOccurrence = nx.part, False
        made[int(obj.Tag)] = b
        return obj

    for kind in ("SurfaceFinishBuilder", "LineWeldBuilder"):
        nx.on_commit(kind, commit)
    nx.part.Annotations.CreateLeaderData = lambda: Node("LeaderData")
    nx.part.Annotations.DraftingSurfaceFinishSymbol = NS(
        CreateDraftingSurfaceFinishSymbolBuilder=factory("SurfaceFinishBuilder")
    )
    nx.part.Annotations.Welds = NS(CreateLineWeldBuilder=factory("LineWeldBuilder"))
    return nx


def test_roughness_takes_units_and_the_origin_defaults_beside_the_leader():
    nx = symbols_nx()
    edge = line_edge(nx, (0, 50, 20), (70, 50, 20))
    install_eval(nx, [edge])
    ok = run(nx, "nx_create_drawing_symbol", view=nx.top.ref, object=edge.ref,
             symbol="surface_finish", roughness_ra="3.2 um", removal="prohibited")  # fmt: skip
    assert ok.ok, ok
    assert ok.data["text"] == ["Ra 3.2"]
    assert ok.data["symbol"] == {"type": "surface_finish", "roughness_ra": 3.2,
                                 "removal": "prohibited"}  # fmt: skip
    anchor = ok.data["leader"]["point"]
    assert ok.data["origin"] == pytest.approx([anchor[0] + 20, anchor[1] + 20])
    for text, value in [("Ra 0.8", 0.8), ("12.5", 12.5), (6.3, 6.3), ("1.6 microns", 1.6)]:
        assert annotations.roughness(text) == value
    bad = run(nx, "nx_create_drawing_symbol", view=nx.top.ref, object=edge.ref,
              symbol="surface_finish", roughness_ra="N8")  # fmt: skip
    assert bad.error["field"] == "/roughness_ra" and "um" in bad.error["hint"]
    zero = run(nx, "nx_create_drawing_symbol", view=nx.top.ref, object=edge.ref,
               symbol="surface_finish", roughness_ra=0)  # fmt: skip
    assert zero.error["field"] == "/roughness_ra" and zero.mutation_outcome == "not_started"


def test_a_weld_symbol_echoes_its_type_size_and_side():
    nx = symbols_nx()
    edge = line_edge(nx, (0, 0, 20), (80, 0, 20))
    install_eval(nx, [edge])
    ok = run(nx, "nx_create_drawing_symbol", view=nx.top.ref, object=edge.ref, origin=[60, 40],
             symbol="weld", weld="fillet", size=5, side="both")  # fmt: skip
    assert ok.ok, ok
    assert ok.data["symbol"] == {"type": "weld", "weld": "fillet", "size": 5.0, "side": "both"}
    assert ok.data["text"] == ["5"] and nx.live_builders == 0


# ---- center marks and centerlines -------------------------------------------------------------


class Centerlines(list):
    def __init__(self, nx):
        super().__init__()
        self.nx = nx

    def CreateCenterMarkBuilder(self, existing):
        return self.nx.builder("CenterMarkBuilder", existing)


def marks_nx():
    nx = notes_nx()
    nx.part.Annotations.Centerlines = Centerlines(nx)
    to_drawing = nx.uf.View.MapModelToDrawing

    def commit(b):
        for edge, view, _at in (c[0] for c in b.Locations.Add.calls):
            mark = Object("CENTER_MARK")
            mark.OwningPart, mark.IsOccurrence = nx.part, False
            mark.NumberOfAssociativities = 1
            mark.GetAssociativity = lambda i, e=edge: NS(FirstObject=e)
            mark.AnnotationOrigin = point(*to_drawing(view.Tag, edge.curve[1]), 0.0)
            nx.dependent[mark.Tag] = (1, "SHT1@0")
            nx.part.Annotations.Centerlines.append(mark)
        return Node("CenterMark")

    nx.on_commit("CenterMarkBuilder", commit)
    return nx


def test_center_marks_give_one_origin_each_and_warn_about_a_second_mark():
    nx = marks_nx()
    left, right = arc_edge(nx, (25, 25, 20), 4), arc_edge(nx, (75, 25, 20), 4)
    install_eval(nx, [left, right])
    ok = run(nx, "nx_create_centerline", view=nx.top.ref, kind="center_mark",
             objects=[left.ref, right.ref])  # fmt: skip
    assert ok.ok, ok
    assert ok.data["origins"] == [pytest.approx([125, 125]), pytest.approx([175, 125])]
    assert "origin" not in ok.data and not ok.warnings
    again = run(nx, "nx_create_centerline", view=nx.top.ref, kind="center_mark",
                objects=[left.ref])  # fmt: skip
    assert again.ok, again
    assert any("already had a center mark" in w["message"] for w in again.warnings)


def test_an_axis_out_of_the_view_names_the_views_that_show_it():
    nx = notes_nx()
    side = make_view(
        nx, nx.sheet_obj, "Front@2", pos=(100, 40), axes=((1, 0, 0), (0, 0, 1), (0, -1, 0))
    )
    cylinder = nx.face(normal=(0, 0, 1), origin=(25, 25, 0), kind="cylinder")
    bad = run(nx, "nx_create_centerline", view=nx.top.ref, kind="axis", objects=[cylinder.ref])
    assert bad.error["code"] == "NX_UNSUPPORTED_GEOMETRY"
    assert [v["id"] for v in bad.error["details"]["views"]] == [side.ref]
    assert bad.mutation_outcome == "not_started"

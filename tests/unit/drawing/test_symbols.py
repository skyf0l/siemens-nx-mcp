"""ID, library, target point, intersection and edge condition symbols on the fake."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.drawing import symbols
from tests.kit import run
from tests.kit.fake import Node
from tests.kit.objects import Object, point
from tests.unit.drawing.fakes import arc_edge, drawing_nx, install_eval, line_edge, make_view

COPIED = (
    "Type", "UpperText", "LowerText", "Size", "MasterSymbolPath", "MasterSymbolName", "Method",
    "SymbolElement", "LimitDeviation", "LimitDeviationA1", "LimitDeviationA2", "Height",
    "Extension",
)  # fmt: skip


def symbols_nx(drop=()):
    """A sheet with a top view at (100, 100); every symbol collection makes builders whose
    reopened copies read what the first one stored (``drop``: members NX forgets)."""
    nx = drawing_nx()
    nx.sheet_obj = nx.make_sheet()
    nx.top = make_view(nx, nx.sheet_obj, pos=(100, 100))
    made = {}

    def factory(kind):
        def make(existing):
            b = nx.builder(kind, existing)
            if not isinstance(existing, Object):
                b.Extension, b.Size, b.Height = 3.0, 10.0, 5.0
                for name in ("MasterSymbolPath", "UpperText", "LowerText", "LimitDeviation",
                             "LimitDeviationA1", "LimitDeviationA2"):  # fmt: skip
                    setattr(b, name, "")
                return b
            src = made[int(existing.Tag)]
            for name in COPIED:
                if name in src.__dict__ and name not in drop:
                    setattr(b, name, src.__dict__[name])
                elif name in drop:
                    setattr(b, name, "")
            if "SelectSymbol" in src.__dict__:
                path = src.SelectSymbol.calls[-1][0][0]
                b.MasterSymbolPath, b.MasterSymbolName = path, "Title"
            for name in ("Location", "IntersectionObject1"):
                if name in src.__dict__:
                    value = getattr(src, name).SetValue.calls[-1][0]
                    setattr(b, name, NS(GetValue=lambda value=value: value))
            if "Leader" in src.__dict__ and src.Leader.Leaders.Append.calls:
                data = src.Leader.Leaders.Append.calls[-1][0][0]
                attached = data.Leader.SetValue.calls[-1][0]
                b.Leader = NS(Leaders=NS(Length=1, FindItem=lambda i: NS(
                    Leader=NS(GetValue=lambda: attached))))  # fmt: skip
            return b

        return make

    def commit(b):
        obj = Object("SYMBOL")
        if "Origin" in b.__dict__ and b.Origin.Origin.SetValue.calls:
            obj.AnnotationOrigin = b.Origin.Origin.SetValue.calls[-1][0][2]
        else:  # target point and intersection: NX places them on their geometry
            obj.AnnotationOrigin = point(*nx.spot, 0.0)
        obj.OwningPart, obj.IsOccurrence = nx.part, False
        nx.dependent[obj.Tag] = (1, "SHT1@0")
        made[int(obj.Tag)] = b
        return obj

    kinds = {
        "IdSymbols": ("CreateIdSymbolBuilder", "IdSymbolBuilder"),
        "CustomSymbols": ("CreateDraftingCustomSymbolBuilder", "CustomSymbolBuilder"),
        "TargetPoints": ("CreateTargetPointBuilder", "TargetPointBuilder"),
        "IntersectionSymbols": ("CreateIntersectionSymbolBuilder", "IntersectionSymbolBuilder"),
        "EdgeConditionSymbol": ("CreateEdgeConditionSymbolBuilder", "EdgeConditionBuilder"),
    }
    annotations = NS(PartsLists=[], BendTables=[], CreateLeaderData=lambda: Node("LeaderData"))
    for collection, (method, kind) in kinds.items():
        setattr(annotations, collection, NS(**{method: factory(kind)}))
        nx.on_commit(kind, commit)
    nx.part.Annotations = annotations
    nx.hole = arc_edge(nx, (25, 25, 20), 8)
    nx.back = line_edge(nx, (0, 50, 20), (70, 50, 20))
    nx.right = line_edge(nx, (80, 40, 20), (80, 0, 20))
    nx.front = line_edge(nx, (0, 0, 20), (80, 0, 20))
    install_eval(nx, [nx.hole, nx.back, nx.right, nx.front])
    nx.spot = (0.0, 0.0)
    return nx


@pytest.fixture
def library(tmp_path, monkeypatch):
    folder = tmp_path / "NX_PMI_Symbols"
    folder.mkdir()
    for name in ("balloon_note", "part_identification"):
        (folder / f"{name}.sym.prt").write_bytes(b"")
    monkeypatch.setenv(symbols.LIBRARY_ENV, str(tmp_path))
    return folder


def test_an_id_symbol_reads_back_shape_text_size_and_origin():
    nx = symbols_nx()
    nx.sheet_obj.Open()
    ok = run(nx, "nx_create_sheet_symbol", symbol="id", text="1", origin=[300, 250], size=10)
    assert ok.ok, ok
    assert ok.data["origin"] == pytest.approx([300, 250]) and ok.data["text"] == ["1"]
    assert ok.data["symbol"]["shape"] == "circle" and ok.data["symbol"]["size"] == pytest.approx(10)
    assert ok.data["sheet"]["id"] == nx.sheet_obj.ref and nx.live_builders == 0


def test_a_divided_id_symbol_with_a_leader_and_its_refusals():
    nx = symbols_nx()
    ok = run(nx, "nx_create_sheet_symbol", symbol="id", shape="divided_circle", text="A",
             lower_text="2", view=nx.top.ref, objects=[nx.hole.ref], origin=[60, 160])  # fmt: skip
    assert ok.ok, ok
    assert ok.data["text"] == ["A", "2"] and ok.data["leader"]["object"]["kind"] == "edge"
    for extra, field in [
        ({"text": "A", "lower_text": "2", "origin": [1, 2]}, "/lower_text"),  # circle: one text
        ({"text": "A", "objects": [nx.hole.ref]}, "/view"),
        ({"text": "A"}, "/origin"),
        ({"origin": [1, 2]}, "/text"),
        ({"text": "A", "origin": [1, 2], "deviations": ["+0.1"]}, "/deviations"),
    ]:
        bad = run(nx, "nx_create_sheet_symbol", symbol="id", **extra)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", (extra, bad)


def test_a_library_symbol_is_found_by_name(library):
    nx = symbols_nx()
    nx.sheet_obj.Open()
    ok = run(nx, "nx_create_sheet_symbol", symbol="custom", name="Balloon_Note", origin=[350, 60])
    assert ok.ok, ok
    assert ok.data["symbol"]["name"] == "balloon_note"
    first = next(b for b in nx.builders if b.kind == "CustomSymbolBuilder")
    picked = first.SelectSymbol.calls[-1][0][0]
    assert picked == str(library / "balloon_note.sym.prt")
    missing = run(nx, "nx_create_sheet_symbol", symbol="custom", name="nope", origin=[1, 2])
    assert missing.error["code"] == "NX_NOT_FOUND" and missing.error["field"] == "/name"
    assert missing.error["details"]["names"] == ["balloon_note", "part_identification"]
    led = run(nx, "nx_create_sheet_symbol", symbol="custom", name="balloon_note", origin=[1, 2],
              view=nx.top.ref, objects=[nx.back.ref])  # fmt: skip
    assert led.error["code"] == "NX_UNSUPPORTED_OPTION" and led.mutation_outcome == "not_started"


def test_a_target_point_lands_on_the_circle_center():
    nx = symbols_nx()
    nx.spot = (25.0 + 100, 25.0 + 100)  # the hole center as the top view maps it
    ok = run(nx, "nx_create_sheet_symbol", symbol="target_point", view=nx.top.ref,
             objects=[nx.hole.ref], size=6)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["origin"] == pytest.approx([125, 125])
    assert ok.data["symbol"]["height"] == pytest.approx(6)
    nx.spot = (0.0, 0.0)  # NX put it elsewhere: refused and rolled back
    off = run(nx, "nx_create_sheet_symbol", symbol="target_point", view=nx.top.ref,
              objects=[nx.hole.ref])  # fmt: skip
    assert off.error["code"] == "NX_VERIFICATION_FAILED" and off.mutation_outcome == "rolled_back"
    line = run(nx, "nx_create_sheet_symbol", symbol="target_point", view=nx.top.ref,
               objects=[nx.back.ref])  # fmt: skip
    assert line.error["code"] == "NX_WRONG_GEOMETRY" and line.mutation_outcome == "not_started"
    placed = run(nx, "nx_create_sheet_symbol", symbol="target_point", view=nx.top.ref,
                 objects=[nx.hole.ref], origin=[1, 2])  # fmt: skip
    assert placed.error["field"] == "/origin"


def test_an_intersection_symbol_sits_at_the_virtual_corner():
    nx = symbols_nx()
    assert symbols._line_meet([0, 50], [70, 50], [80, 40], [80, 0]) == pytest.approx([80, 50])
    nx.spot = (180.0, 150.0)
    ok = run(nx, "nx_create_sheet_symbol", symbol="intersection", view=nx.top.ref,
             objects=[nx.back.ref, nx.right.ref])  # fmt: skip
    assert ok.ok, ok
    assert ok.data["origin"] == pytest.approx([180, 150]) and len(ok.data["symbol"]["objects"]) == 2
    parallel = run(nx, "nx_create_sheet_symbol", symbol="intersection", view=nx.top.ref,
                   objects=[nx.back.ref, nx.front.ref])  # fmt: skip
    assert parallel.error["field"] == "/objects" and parallel.mutation_outcome == "not_started"
    one = run(nx, "nx_create_sheet_symbol", symbol="intersection", view=nx.top.ref,
              objects=[nx.back.ref])  # fmt: skip
    assert one.error["field"] == "/objects"


def test_edge_condition_deviations_follow_their_method():
    nx = symbols_nx()
    size = run(nx, "nx_create_sheet_symbol", symbol="edge_condition", view=nx.top.ref,
               objects=[nx.back.ref], condition="size", deviations=["+0.3", "-0.1"])  # fmt: skip
    assert size.ok, size
    assert size.data["symbol"] == {"type": "edge_condition", "condition": "size",
                                   "deviations": ["+0.3", "-0.1"]}  # fmt: skip
    b = nx.last("EdgeConditionBuilder")
    assert (b.LimitDeviationA1, b.LimitDeviationA2) == ("+0.3", "-0.1")
    anchor = size.data["leader"]["point"]
    assert size.data["origin"] == pytest.approx([anchor[0] + 20, anchor[1] + 20])
    both = run(nx, "nx_create_sheet_symbol", symbol="edge_condition", view=nx.top.ref,
               objects=[nx.back.ref], condition="bilateral", deviations=["0.2"])  # fmt: skip
    assert both.data["symbol"]["deviations"] == ["0.2"]
    for args, field in [
        ({"condition": "size"}, "/deviations"),
        ({"condition": "bilateral", "deviations": ["+0.2"]}, "/deviations/0"),
        ({"condition": "minus", "deviations": ["0.2"]}, "/deviations"),
        ({"condition": "size", "deviations": ["0.2 mm"]}, "/deviations/0"),
    ]:
        bad = run(nx, "nx_create_sheet_symbol", symbol="edge_condition", view=nx.top.ref,
                  objects=[nx.back.ref], **args)  # fmt: skip
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", (args, bad)


def test_a_deviation_nx_drops_fails_the_read_back():
    nx = symbols_nx(drop=("LimitDeviationA2",))
    bad = run(nx, "nx_create_sheet_symbol", symbol="edge_condition", view=nx.top.ref,
              objects=[nx.back.ref], condition="size", deviations=["+0.3", "-0.1"])  # fmt: skip
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"
    assert nx.live_builders == 0


def test_the_drafting_license_is_asked_before_nx():
    nx = symbols_nx()
    nx.session.LicenseManager = NS(CheckPresence=lambda feature: False)
    nx.sheet_obj.Open()
    bad = run(nx, "nx_create_sheet_symbol", symbol="id", text="1", origin=[300, 250])
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and bad.mutation_outcome != "committed"
    assert not nx.builders

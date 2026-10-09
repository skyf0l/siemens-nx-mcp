"""Drafting GD&T on view geometry (nx_create_gdt view_* kinds) on the fake."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.domains.drawing import gdt, hooks
from tests.kit import run
from tests.kit.fake import Node
from tests.kit.objects import Object
from tests.unit.drawing.fakes import drawing_nx, install_eval, line_edge, make_view

KINDS = {
    "DraftingFeatureControlFrameBuilder": "DraftingFcf",
    "DraftingDatumFeatureSymbolBuilder": "DraftingDatum",
    "DraftingDatumTargetBuilder": "DraftingPointTarget",
}


class Expr:
    """A builder expression: RightHandSide text drives Value."""

    def __init__(self, value=10.0):
        self.Value = value

    @property
    def RightHandSide(self):
        return repr(self.Value)

    @RightHandSide.setter
    def RightHandSide(self, text):
        self.Value = float(text)


def gdt_nx(sheet_units="mm"):
    nx = drawing_nx()
    nx.sheet_obj = nx.make_sheet(units=sheet_units)
    nx.top = make_view(nx, nx.sheet_obj, pos=(100, 100))
    nx.made = {}  # annotation tag -> the builder that created it
    nx.stored = {}  # overrides of read-back values: {member: value}

    def factory(kind):
        def make(existing):
            b = nx.builder(kind, existing)
            if existing is None:
                row = Node("row")
                b.FeatureControlFrameDataList = NS(FindItem=lambda i: row)
                for name in ("Width", "Height", "OuterDiameter", "InnerDiameter"):
                    setattr(b, name, Expr())
                return b
            src = nx.made[int(existing.Tag)]
            data = src.Leader.Leaders.Append.calls[-1][0][0]
            attached = data.Leader.SetValue.calls[-1][0]
            b.Leader = NS(Leaders=NS(Length=1, FindItem=lambda i: NS(
                Leader=NS(GetValue=lambda: attached))))  # fmt: skip
            for name in (
                "Characteristic",
                "FeatureControlFrameDataList",
                "Letter",
                "Type",
                "Label",
                "Index",
                "Width",
                "Height",
                "OuterDiameter",
                "InnerDiameter",
            ):
                setattr(b, name, nx.stored.get(name, getattr(src, name)))  # fmt: skip
            return b

        return make

    def commit(b):
        obj = type(KINDS[b.kind], (Object,), {})(KINDS[b.kind])
        origin = b.Origin.Origin.SetValue.calls[-1][0][2]
        obj.GetText, obj.AnnotationOrigin = (lambda: ["<&70>TEXT"]), origin
        obj.OwningPart, obj.IsOccurrence = nx.part, False
        nx.made[int(obj.Tag)] = b
        nx.dependent[obj.Tag] = (1, f"{nx.sheet_obj.Name}@0")
        return obj

    for kind in KINDS:
        nx.on_commit(kind, commit)
    nx.part.Annotations = NS(
        PartsLists=[],
        BendTables=[],
        CreateLeaderData=lambda: Node("LeaderData"),
        CreateDraftingFeatureControlFrameBuilder=factory("DraftingFeatureControlFrameBuilder"),
        CreateDraftingDatumTargetBuilder=factory("DraftingDatumTargetBuilder"),
        Datums=NS(
            CreateDraftingDatumFeatureSymbolBuilder=factory("DraftingDatumFeatureSymbolBuilder")
        ),
    )
    nx.front = line_edge(nx, (0, 0, 20), (80, 0, 20))
    nx.hole = line_edge(nx, (20, 10, 20), (20, 30, 20))
    install_eval(nx, [nx.front, nx.hole])
    return nx


def view_args(nx, obj, origin, **extra):
    return {"view": nx.top.ref, "object": obj.ref, "origin": origin, **extra}


def test_frame_with_datum_letters_is_read_back_with_leader_and_origin():
    nx = gdt_nx()
    datum = run(nx, "nx_create_gdt", kind="view_datum_feature", letter="A",
                **view_args(nx, nx.front, [60, 40]))  # fmt: skip
    assert datum.ok, datum
    assert datum.data["letter"] == "A" and datum.data["origin"] == pytest.approx([60, 40])
    frame = {"characteristic": "position", "tolerance": 0.1, "material": "mmc",
             "zone": "diameter", "datums": [datum.data["annotation"]["id"]],
             "datum_material": ["mmc"], "projected_height": 5}  # fmt: skip
    ok = run(nx, "nx_create_gdt", kind="view_fcf", frame=frame,
             **view_args(nx, nx.hole, [150, 120]))  # fmt: skip
    assert ok.ok, ok
    assert ok.data["frame"] == {
        "characteristic": "position", "tolerance": 0.1, "material": "mmc", "zone": "diameter",
        "datums": ["A"], "datum_material": ["mmc"], "projected_height": 5.0,
    }  # fmt: skip
    leader = ok.data["leader"]
    assert leader["object"]["kind"] == "edge" and leader["view"]["id"] == nx.top.ref
    attached = next(b for b in nx.builders if b.kind == "DraftingFeatureControlFrameBuilder")
    assert ok.data["origin"] == pytest.approx([150, 120]) and ok.data["text"] == "<&70>TEXT"
    assert ok.data["sheet"]["kind"] == "drawing_sheet"
    assert nx.made and attached.destroyed and nx.live_builders == 0


def test_datum_target_sizes_on_an_inch_sheet():
    nx = gdt_nx(sheet_units="in")
    ok = run(nx, "nx_create_gdt", kind="view_datum_target", letter="B",
             target={"type": "rectangular", "index": 2, "width": 8, "height": 4},
             **view_args(nx, nx.front, [5, 6]))  # fmt: skip
    assert ok.ok, ok
    assert ok.units["sheet_length"] == "in" and ok.data["origin"] == pytest.approx([5, 6])
    assert ok.data["target"] == {"type": "rectangular", "label": "B", "index": 2,
                                 "width": 8.0, "height": 4.0}  # fmt: skip
    made = next(b for b in nx.builders if b.kind == "DraftingDatumTargetBuilder")
    placed = made.Origin.Origin.SetValue.calls[-1][0][2]
    assert pytest.approx((127.0, 152.4)) == (placed.X, placed.Y)  # NX takes part units


def test_a_definition_nx_did_not_store_rolls_back():
    nx = gdt_nx()
    nx.stored["Letter"] = "Z"
    bad = run(nx, "nx_create_gdt", kind="view_datum_feature", letter="A",
              **view_args(nx, nx.front, [60, 40]))  # fmt: skip
    assert bad.error["code"] == "NX_VERIFICATION_FAILED"
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0


def test_each_kind_takes_its_own_fields_before_nx():
    nx = gdt_nx()
    face = nx.face()
    flat = {"characteristic": "flatness", "tolerance": 0.05}
    cases = [
        ({"kind": "view_fcf", "view": nx.top.ref, "object": nx.front.ref, "frame": flat},
         "/origin"),
        ({"kind": "view_fcf", "frame": flat, "faces": [face.ref],
          **view_args(nx, nx.front, [1, 2])}, "/faces"),
        ({"kind": "view_fcf", "frame": flat, "letter": "A", **view_args(nx, nx.front, [1, 2])},
         "/letter"),
        ({"kind": "view_datum_feature", "letter": "A", "frame": flat,
          **view_args(nx, nx.front, [1, 2])}, "/frame"),
        ({"kind": "view_datum_target", "letter": "A", **view_args(nx, nx.front, [1, 2])},
         "/target"),
        ({"kind": "view_datum_feature", "letter": "A", "target": {"type": "point"},
          **view_args(nx, nx.front, [1, 2])}, "/target"),
        ({"kind": "fcf", "frame": flat, "faces": [face.ref], "position": [0, 0, 0],
          "view": nx.top.ref}, "/view"),
        ({"kind": "fcf", "frame": flat, "view": nx.top.ref, "object": nx.front.ref,
          "origin": [1, 2]}, "/faces"),
    ]  # fmt: skip
    for args, field in cases:
        bad = run(nx, "nx_create_gdt", **args)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", (args, bad)
    wrong = run(nx, "nx_create_gdt", kind="view_fcf", frame={**flat, "characteristic": "position",
                "datums": [nx.top.ref]}, **view_args(nx, nx.front, [1, 2]))  # fmt: skip
    assert wrong.error["category"] == "wrong_kind" and wrong.mutation_outcome == "not_started"
    assert nx.live_builders == 0


def test_datum_target_rules():
    gdt.check_target({"type": "point"})
    gdt.check_target({"type": "annular", "diameter": 10.0, "inner_diameter": 4.0})
    for target, field in [
        ({"type": "circular"}, "/target/diameter"),
        ({"type": "rectangular", "width": 2.0}, "/target/height"),
        ({"type": "point", "width": 3.0}, "/target/width"),
        ({"type": "line", "diameter": 3.0}, "/target/diameter"),
        ({"type": "circular", "diameter": -1.0}, "/target/diameter"),
        ({"type": "annular", "diameter": 4.0, "inner_diameter": 6.0}, "/target/inner_diameter"),
    ]:
        with pytest.raises(NXError) as error:
            gdt.check_target(target)
        assert error.value.field == field, target


def test_annotation_listing_includes_drafting_gdt_and_targets():
    frame, target = Object("FCF"), Object("TARGET")
    part = NS(Notes=[], Labels=[], Gdts=[frame],
              Annotations=NS(Datums=[], Fcfs=[], DraftingDatumTargets=[target]))  # fmt: skip
    assert {o.Tag for o in hooks._annotations(part)} == {frame.Tag, target.Tag}

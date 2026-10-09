"""Blend, chamfer, shell, draft, boolean, mirror, pattern, trim, scale, transform, face edits
and materials on the shared fake, through the real kernel."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.kit.objects import Object
from tests.unit.modeling.fakes import call, feature, modeling_nx, new_body


def changes(nx, kind, body, volume, result=None):
    def hook(b):
        body.volume = volume
        return feature(nx, kind, result if result is not None else [body])

    nx.on_commit(kind, hook)


def test_constant_blend_and_volume_readback():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    changes(nx, "EdgeBlendBuilder", block, 7990.0)
    ok = run(nx, "nx_create_blend", edges=[block.edges[0].ref], radius="0.1 in")
    assert ok.ok, ok
    b = nx.last("EdgeBlendBuilder")
    assert b.AddChainset.calls[0][0][1] == repr(2.54) and b.Tolerance == 0.001
    changes(nx, "EdgeBlendBuilder", block, block.volume)
    same = run(nx, "nx_create_blend", edges=[nx.ref_of(block.edges[1], "edge")], radius=2)
    assert same.error["details"]["check"] == "volume" and same.mutation_outcome == "rolled_back"


def test_variable_blend_points_and_rules():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    changes(nx, "EdgeBlendBuilder", block, 7900.0)
    edge = block.edges[0].ref
    ok = run(nx, "nx_create_blend", edges=[edge],
             variable=[{"at": 1, "radius": 4}, {"at": 0, "radius": 2}])  # fmt: skip
    assert ok.ok, ok
    data = nx.last("EdgeBlendBuilder").AddVariableRadiusDataNew.calls
    assert [c[0][1:3] for c in data] == [("0.0", "2.0"), ("100.0", "4.0")]
    edge = nx.ref_of(block.edges[0], "edge")
    two_edges = run(nx, "nx_create_blend", edges=[edge, nx.ref_of(block.edges[1], "edge")],
                    variable=[{"at": 0, "radius": 2}, {"at": 1, "radius": 3}])  # fmt: skip
    assert two_edges.error["field"] == "/edges" and two_edges.mutation_outcome == "not_started"
    both = run(nx, "nx_create_blend", edges=[edge], radius=1,
               variable=[{"at": 0, "radius": 2}, {"at": 1, "radius": 3}])  # fmt: skip
    assert both.error["category"] == "invalid_argument"
    other = nx.block(5, 5, 5, origin=(100, 0, 0))
    mixed = run(nx, "nx_create_blend", edges=[edge, other.edges[0].ref], radius=1)
    assert mixed.error["field"] == "/edges"


@pytest.mark.parametrize(
    ("args", "option"),
    [
        ({"method": "symmetric", "offset": 2}, "SymmetricOffsets"),
        ({"method": "two_offsets", "offset": 2, "offset2": 4, "reverse": True}, "TwoOffsets"),
        ({"method": "offset_angle", "offset": 3, "angle": 30}, "OffsetAndAngle"),
    ],
)
def test_chamfer_methods(args, option):
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    changes(nx, "ChamferBuilder", block, 7900.0)
    ok = run(nx, "nx_create_chamfer", edges=[block.edges[0].ref], **args)
    assert ok.ok, ok
    b = nx.last("ChamferBuilder")
    assert b.Option is getattr(nx.nx.Features.ChamferBuilder.ChamferOption, option)
    assert b.ReverseOffsets is bool(args.get("reverse"))


@pytest.mark.parametrize(
    "args",
    [
        {"method": "symmetric", "offset": 2, "angle": 30},
        {"method": "two_offsets", "offset": 2},
        {"method": "offset_angle", "offset": 2, "angle": 95},
        {"method": "symmetric", "offset": 2, "reverse": True},
        {"method": "symmetric", "offset": -1},
    ],
)
def test_chamfer_argument_errors(args):
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    bad = run(nx, "nx_create_chamfer", edges=[block.edges[0].ref], **args)
    assert bad.error["category"] == "invalid_argument" and bad.mutation_outcome == "not_started"


def test_shell_and_draft():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    top = block.face((0, 0, 1))
    changes(nx, "ShellBuilder", block, 2000.0)
    ok = run(nx, "nx_create_shell", body=block.ref, thickness=2, remove_faces=[top.ref])
    assert ok.ok and nx.last("ShellBuilder").DefaultThicknessFlip is True, ok
    other = nx.block(5, 5, 5, origin=(100, 0, 0))
    bad = run(nx, "nx_create_shell", body=block.ref, thickness=2,
              remove_faces=[nx.ref_of(other.face((0, 0, 1)), "face")])  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_OWNER" and bad.changes["references"] == "preserved"
    side = nx.ref_of(block.face((1, 0, 0)), "face")
    bottom = nx.ref_of(block.face((0, 0, -1)), "face")
    changes(nx, "DraftBuilder", block, 1950.0)
    drafted = run(nx, "nx_create_draft", faces=[side], stationary_face=bottom,
                  direction={"principal": "z"}, angle=5)  # fmt: skip
    assert drafted.ok, drafted
    assert nx.last("DraftBuilder").FaceSetAngleExpressionList.Append.calls[0][0][0].args[1] == "5.0"
    bottom = nx.ref_of(block.face((0, 0, -1)), "face")
    fixed = run(nx, "nx_create_draft", faces=[bottom], stationary_face=bottom,
                direction=[0, 0, 1], angle=5)  # fmt: skip
    assert fixed.error["field"] == "/faces/0"


def test_boolean_operations_and_rules():
    nx = modeling_nx()
    a = nx.block(10, 10, 10)
    b = nx.block(10, 10, 10, origin=(5, 0, 0))
    changes(nx, "BooleanBuilder", a, 1500.0)
    ok = run(nx, "nx_create_boolean", operation="unite", target=a.ref, tools=[b.ref])
    assert ok.ok and ok.data["volume"] == 1500, ok
    assert nx.last("BooleanBuilder").Operation is nx.nx.Features.FeatureBooleanType.Unite
    self_tool = run(nx, "nx_create_boolean", operation="subtract", target=a.ref, tools=[a.ref])
    assert self_tool.error["field"] == "/tools"
    changes(nx, "BooleanBuilder", a, 2000.0)
    grew = run(nx, "nx_create_boolean", operation="subtract", target=a.ref, tools=[b.ref])
    assert grew.error["details"]["check"] == "volume"


def test_mirror_checks_the_reflection_and_hides_sources():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)

    def mirrored(b):
        return feature(nx, "MIRROR_BODY", [new_body(nx, [-40, 0, 0, 0, 20, 10])])

    nx.on_commit("MirrorBodyBuilder", mirrored)
    ok = run(nx, "nx_create_mirror", bodies=[block.ref], plane={"principal": "yz"},
             keep_original=False)  # fmt: skip
    assert ok.ok, ok
    assert block.IsBlanked and ok.data["volume"] == 8000
    nx.on_commit("MirrorBodyBuilder", lambda b: feature(nx, "MIRROR_BODY",
                 [new_body(nx, [0, 0, 0, 40, 20, 10])]))  # fmt: skip
    bad = run(nx, "nx_create_mirror", bodies=[block.ref],
              plane={"origin": [0, 0, 0], "normal": [1, 0, 0]})  # fmt: skip
    assert bad.error["details"]["check"] == "mirror[0]" and bad.mutation_outcome == "rolled_back"


def pattern_nx():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    seed = feature(nx, "HOLE PACKAGE", [block])
    counts = {}

    def commit(b):
        counts["x"] = float(b.PatternService.RectangularDefinition.XSpacing.NCopies.RightHandSide)
        block.volume -= 50
        return feature(nx, "PATTERN_FEATURE", [block])

    def builder(kind, *args):
        made = make(kind, *args)
        if kind == "PatternFeatureBuilder" and args and args[0] is not None:
            rect = made.PatternService.RectangularDefinition
            rect.XSpacing.NCopies.Value = counts["x"] + nx.miscount
            rect.YSpacing.NCopies.Value = 1
        return made

    make = nx.builder
    nx.builder = builder
    nx.miscount = 0
    nx.on_commit("PatternFeatureBuilder", commit)
    return nx, block, seed


def test_linear_pattern_reads_back_the_count():
    nx, block, seed = pattern_nx()
    layout = {"linear": {"direction": [1, 0, 0], "count": 3, "spacing": "0.5 in"}}
    ok = run(nx, "nx_create_pattern", features=[nx.ref_of(seed, "feature")], layout=layout)
    assert ok.ok and ok.data["count"] == 3, ok
    d = next(b for b in nx.builders if b.commits).PatternService.RectangularDefinition
    assert d.XSpacing.PitchDistance.RightHandSide == repr(12.7)
    nx.miscount = 1
    bad = run(nx, "nx_create_pattern", features=[nx.ref_of(seed, "feature")], layout=layout)
    assert bad.error["details"]["check"] == "count" and nx.live_builders == 0


@pytest.mark.parametrize(
    "layout",
    [
        {"circular": {"axis": {"principal": "z"}, "count": 7, "angle": 60}},
        {"linear": {"direction": [1, 0, 0], "count": 2, "spacing": 5, "count2": 2}},
        {
            "linear": {
                "direction": [1, 0, 0],
                "count": 2,
                "spacing": 5,
                "direction2": [2, 0, 0],
                "count2": 2,
                "spacing2": 3,
            }
        },  # fmt: skip
    ],
)
def test_pattern_layout_errors(layout):
    nx, block, seed = pattern_nx()
    bad = run(nx, "nx_create_pattern", features=[nx.ref_of(seed, "feature")], layout=layout)
    assert bad.error["category"] == "invalid_argument", bad
    assert nx.live_builders == 0 and not any(b.commits for b in nx.builders)


def test_trim_keeps_the_requested_side_and_split_adds_up():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    plane = nx.datum_plane(origin=(10, 0, 0), normal=(1, 0, 0))

    def trimmed(b):
        block.box, block.volume, block.centroid = [0, 0, 0, 10, 20, 10], 2000.0, None
        return feature(nx, "TRIM_BODY", [block])

    nx.on_commit("TrimBody2Builder", trimmed)
    ok = call(nx, "nx_create_trim_body", {"body": block.ref, "tool": plane.ref, "keep": "back"})
    assert ok.ok and ok.data["volumes"] == [2000.0], ok
    assert nx.last("TrimBody2Builder").BooleanTool.ReverseDirection is False
    block.box, block.volume = [0, 0, 0, 40, 20, 10], 8000.0
    wrong = call(nx, "nx_create_trim_body", {"body": block.ref, "tool": plane.ref, "keep": "front"})
    assert (
        wrong.error["details"]["check"] == "kept_side" and wrong.mutation_outcome == "rolled_back"
    )
    block.box, block.volume = [0, 0, 0, 40, 20, 10], 8000.0
    nx.on_commit("SplitBodyBuilder", lambda b: feature(nx, "SPLIT_BODY", [
        new_body(nx, [0, 0, 0, 10, 20, 10]), new_body(nx, [10, 0, 0, 40, 20, 10])]))  # fmt: skip
    # The rollback invalidated every reference of the part: reacquire them.
    body, tool = nx.ref_of(block, "body"), nx.ref_of(plane, "datum_plane")
    split = call(nx, "nx_create_trim_body", {"body": body, "tool": tool, "keep": "both"})
    assert split.ok and split.data["volumes"] == [2000.0, 6000.0], split
    miss = nx.datum_plane(origin=(50, 0, 0), normal=(1, 0, 0))
    bad = call(nx, "nx_create_trim_body", {"body": body, "tool": miss.ref, "keep": "back"})
    assert "does not cut" in bad.error["message"] and bad.changes["references"] == "preserved"


def test_scale_checks_volume_and_fixed_point():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)

    def scaled(b):
        block.box, block.volume = [0, 0, 0, 20, 20, 20], 8000.0
        return feature(nx, "SCALE", [block])

    nx.on_commit("ScaleBuilder", scaled)
    ok = run(nx, "nx_create_scale", bodies=[block.ref], factor=2)
    assert ok.ok, ok
    block.box, block.volume = [0, 0, 0, 10, 10, 10], 1000.0
    off = run(nx, "nx_create_scale", bodies=[block.ref], factor=2, origin=[10, 10, 10])
    assert off.error["details"]["check"] == "centroid[0]"
    one = run(nx, "nx_create_scale", bodies=[block.ref], factor=1)
    assert one.error["field"] == "/factor" and one.mutation_outcome == "not_started"


def test_transform_moves_and_the_definition_hook_edits():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    nx.nx.Features.MoveObject = Object

    def moved(b):
        to = b.TransformMotion.ToCsys.args[0]
        block.box = [to.X, to.Y, to.Z, to.X + 10, to.Y + 10, to.Z + 10]
        return feature(nx, "MOVE_OBJECT", [block])

    nx.on_commit("MoveObjectBuilder", moved)
    ok = run(nx, "nx_create_transform", bodies=[block.ref],
             transform={"translation": [20, 0, 0]})  # fmt: skip
    assert ok.ok, ok
    rot = run(nx, "nx_create_transform", bodies=[block.ref],
              transform={"translation": [0, 0, 0], "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 2]]})  # fmt: skip
    assert rot.error["field"].startswith("/transform/rotation")
    move = next(f for f in nx.part.Features if f.FeatureType == "MOVE_OBJECT")
    edit = run(nx, "nx_edit_feature", feature=nx.ref_of(move, "feature"),
               definition={"transform": {"translation": [30, 0, 0]}})  # fmt: skip
    assert edit.ok, edit
    assert nx.builders[-1].args == (move,)


def test_edit_faces_arguments_and_consistency():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    top = block.face((0, 0, 1))
    changes(nx, "OffsetFaceBuilder", block, 8800.0)
    ok = run(nx, "nx_edit_faces", faces=[top.ref], action="offset", distance=1)
    assert ok.ok and nx.last("OffsetFaceBuilder").Direction is False, ok
    bad = run(nx, "nx_edit_faces", faces=[nx.ref_of(top, "face")], action="offset")
    assert bad.error["field"] == "/distance" and bad.mutation_outcome == "not_started"
    extra = run(nx, "nx_edit_faces", faces=[nx.ref_of(top, "face")], action="delete", distance=1)
    assert extra.error["field"] == "/distance"
    changes(nx, "DeleteFaceBuilder", block, 7000.0)
    nx.faults[int(block.Tag)] = 1
    broken = run(nx, "nx_edit_faces", faces=[nx.ref_of(top, "face")], action="delete")
    assert (
        broken.error["details"]["check"] == "consistency"
        and broken.mutation_outcome == "rolled_back"
    )
    assert nx.last("DeleteFaceBuilder").Heal is True


def test_materials_assign_verify_and_read():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    assigned = {}

    class Material:
        def __init__(self, name, density):
            self.Name, self.density = name, density

        def AssignObjects(self, bodies):
            for b in bodies:
                nx.densities[int(b.Tag)] = self.density
                assigned[int(b.Tag)] = self

    library = type(
        "Materials", (list,), {
            "CreatePhysicalMaterialBuilder": lambda self, kind: nx.builder("PhysicalMaterialBuilder", kind),
            "AskMaterialOfObject": lambda self, b: assigned.get(int(b.Tag)),
        })()  # fmt: skip
    nx.part.MaterialManager = NS(PhysicalMaterials=library)

    def committed(b):
        density = b.PropertyTable.SetBaseScalarWithDataPropertyValue.calls[0][0][1]
        made = Material(b.Name, density)
        library.append(made)
        return made

    nx.nx.UF = NS(Modl=NS(DensityUnits=NS(KILOGRAMS_METERS=1)))
    nx.on_commit("PhysicalMaterialBuilder", committed)
    spec = {"custom": {"name": "AL6061", "density": 2700}}
    ok = run(nx, "nx_set_material", bodies=[block.ref], material=spec)
    assert ok.ok and ok.data["items"][0]["density"] == 2700, ok
    again = run(nx, "nx_set_material", bodies=[block.ref], material=spec)
    assert again.ok and len(library) == 1
    clash = run(nx, "nx_set_material", bodies=[block.ref],
                material={"custom": {"name": "al6061", "density": 1000}})  # fmt: skip
    assert clash.error["code"] == "NX_ALREADY_EXISTS" and clash.mutation_outcome == "rolled_back"
    read = run(nx, "nx_get_material")
    assert (
        read.data["items"][0]["material"] == "AL6061" and read.mutation_outcome == "not_applicable"
    )

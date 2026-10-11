"""Blend, chamfer, shell, draft, boolean, trim, scale, transform, face edits and materials
(mirror and pattern: test_patterns) on the shared fake, through the real kernel."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.kit import geom
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
        # method omitted: inferred from the fields given
        ({"offset": "2mm"}, "SymmetricOffsets"),
        ({"offset": 2, "offset2": 3}, "TwoOffsets"),
        ({"offset": 2, "angle": "30deg", "reverse": True}, "OffsetAndAngle"),
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
        {"offset": 2, "offset2": 3, "angle": 30},
    ],
)
def test_chamfer_argument_errors(args):
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    bad = run(nx, "nx_create_chamfer", edges=[block.edges[0].ref], **args)
    assert bad.error["category"] == "invalid_argument" and bad.mutation_outcome == "not_started"


@pytest.mark.parametrize(
    ("args", "field", "words"),
    [
        ({"method": "two_offsets", "offset": 3}, "/offset2", "offset2"),
        ({"method": "symmetric", "offset": 2, "offset2": 3}, "/offset2", "two_offsets"),
        ({"method": "offset_angle", "offset": 2, "angle": 95}, "/angle", "angle"),
        ({"offset": 2, "offset2": 3, "angle": 30}, "/angle", "method"),
    ],
)
def test_chamfer_errors_point_at_the_field_and_say_what_to_send(args, field, words):
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    bad = run(nx, "nx_create_chamfer", edges=[block.edges[0].ref], **args)
    assert bad.error["field"] == field, bad
    assert words in bad.error["hint"], bad


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
    side = nx.ref_of(block.face((1, 0, 0)), "face")
    changes(nx, "DraftBuilder", block, 1900.0)
    bare = run(nx, "nx_create_draft", faces=[side], stationary_face=bottom,
               direction="-z", angle="-3deg")  # fmt: skip
    assert bare.ok, bare  # a bare principal name is a direction
    assert (
        nx.last("DraftBuilder").FaceSetAngleExpressionList.Append.calls[0][0][0].args[1] == "-3.0"
    )
    bottom = nx.ref_of(block.face((0, 0, -1)), "face")
    zero = run(nx, "nx_create_draft", faces=[side], stationary_face=bottom,
               direction="z", angle=0)  # fmt: skip
    assert zero.error["field"] == "/angle" and "deg" in zero.error["hint"]
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
    assert ok.data["deleted_bodies"] == 0  # the fake keeps the tool body
    assert nx.last("BooleanBuilder").Operation is nx.nx.Features.FeatureBooleanType.Unite
    self_tool = run(nx, "nx_create_boolean", operation="subtract", target=a.ref, tools=[a.ref])
    assert self_tool.error["field"] == "/tools"
    changes(nx, "BooleanBuilder", a, 2000.0)
    grew = run(nx, "nx_create_boolean", operation="subtract", target=a.ref, tools=[b.ref])
    assert grew.error["details"]["check"] == "volume"


def test_boolean_counts_the_consumed_tools_and_lists_the_target_first():
    nx = modeling_nx()
    a = nx.block(10, 10, 10)
    tools = [nx.block(10, 10, 10, origin=(5 * k, 0, 0)) for k in range(1, 5)]

    def united(b):
        for t in tools:
            nx.part.Bodies.remove(t)
        a.volume = 3000.0
        return feature(nx, "UNITE", [*tools[:0], a])

    nx.on_commit("BooleanBuilder", united)
    ok = run(nx, "nx_create_boolean", operation="unite", target=a.ref, tools=[t.ref for t in tools])
    assert ok.ok, ok
    assert ok.data["deleted_bodies"] == 4 and [b["id"] for b in ok.data["bodies"]] == [a.ref]
    assert len(ok.changes["deleted"]) == 4


def test_trim_keeps_the_requested_side_and_split_adds_up():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    plane = nx.datum_plane(origin=(10, 0, 0), normal=(1, 0, 0))

    def trimmed(b):
        block.box, block.volume, block.centroid = [0, 0, 0, 10, 20, 10], 2000.0, None
        return feature(nx, "TRIM_BODY", [block])

    nx.on_commit("TrimBody2Builder", trimmed)
    ok = call(nx, "nx_create_trim_body", {"body": block.ref, "tool": plane.ref})  # keep: back
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
    assert "both signs" in bad.error["hint"]


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

        def GetPropTable(self):
            if self.Name == "OLD":  # an older material: no readable property table
                raise RuntimeError("no property table")
            g_cc = NS(Tag=7)  # stored in g/cm^3: converted to kg/m^3

            def set_density(name, value, unit):
                self.density = value

            return NS(
                GetBaseScalarWithDataPropertyValue=lambda name: (self.density / 1000, g_cc),
                SetBaseScalarWithDataPropertyValue=set_density,
            )

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
    nx.part.UnitCollection = NS(FindObject=lambda name: NS(Tag=1),
                                Convert=lambda src, dst, v: v * 1000)  # fmt: skip
    edited = run(nx, "nx_set_material", bodies=[block.ref],
                 material={"custom": {"name": "al6061", "density": 1000}})  # fmt: skip
    assert edited.ok and edited.data["items"][0]["density"] == 1000, edited
    assert "was 2700" in edited.warnings[0]["message"] and len(library) == 1
    read = run(nx, "nx_get_material")
    assert (
        read.data["items"][0]["material"] == "AL6061" and read.mutation_outcome == "not_applicable"
    )
    # The fake's undo does not restore this assignment record: checked last.
    library.append(Material("OLD", 500))
    old = run(nx, "nx_set_material", bodies=[block.ref],
              material={"custom": {"name": "old", "density": 1000}})  # fmt: skip
    assert old.error["code"] == "NX_ALREADY_EXISTS" and old.mutation_outcome == "rolled_back"


# ---------------------------------------------------------------------------
# Live tester batch b25: guidance and short forms


def test_boolean_refuses_a_tool_apart_from_the_target_before_nx():
    nx = modeling_nx()
    a = nx.block(10, 10, 10)
    far = nx.block(10, 10, 10, origin=(50, 0, 0))
    bad = run(nx, "nx_create_boolean", operation="unite", target=a.ref, tools=[far.ref])
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/tools/0", bad
    assert "nx_create_transform" in bad.error["hint"]
    assert not any(b.kind == "BooleanBuilder" for b in nx.builders)
    # A tool that touches another tool of the call (a chain) is not apart.
    near = nx.block(10, 10, 10, origin=(10, 0, 0))
    bridge = nx.block(31, 10, 10, origin=(20, 0, 0))
    changes(nx, "BooleanBuilder", a, 3000.0)
    tools = [nx.ref_of(far, "body"), near.ref, bridge.ref]
    ok = run(nx, "nx_create_boolean", operation="unite", target=a.ref, tools=tools)
    assert ok.ok, ok


def test_boolean_unite_that_leaves_two_bodies_fails_and_rolls_back():
    # NX 2406 commits a unite of bodies that do not meet with the tool kept as a second body.
    nx = modeling_nx()
    a = nx.block(10, 10, 10)
    b = nx.block(10, 10, 10, origin=(5, 0, 0))

    def apart(builder):
        nx.part.Bodies.remove(b)
        return feature(nx, "UNITE", [a, new_body(nx, [5, 0, 0, 15, 10, 10])])

    nx.on_commit("BooleanBuilder", apart)
    bad = run(nx, "nx_create_boolean", operation="unite", target=a.ref, tools=[b.ref])
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["details"]["bodies"] == 2, bad
    assert bad.mutation_outcome == "rolled_back" and "touch" in bad.error["hint"]


def test_boolean_circular_dependency_names_the_associative_copy():
    nx = modeling_nx()
    a = nx.block(10, 10, 10)
    b = nx.block(10, 10, 10, origin=(5, 0, 0))
    nx.refuse("BooleanBuilder", 670015, "Circular dependency detected")
    bad = run(nx, "nx_create_boolean", operation="subtract", target=a.ref, tools=[b.ref])
    assert bad.error["code"] == "NX_BUILD_FAILED", bad
    assert "derived from the target" in bad.error["hint"], bad


def test_scale_about_the_center_keeps_the_centroid():
    nx = modeling_nx()
    block = nx.block(10, 10, 10, origin=(10, 0, 0))

    def scaled(b):
        block.volume, block.centroid = 8000.0, [15.0, 5.0, 5.0]
        return feature(nx, "SCALE", [block])

    nx.on_commit("ScaleBuilder", scaled)
    ok = run(nx, "nx_create_scale", bodies=[block.ref], factor=2, origin="center")
    assert ok.ok and ok.data["volume"] == 8000, ok
    block.volume, block.centroid = 1000.0, None
    moved = run(nx, "nx_create_scale", bodies=[nx.ref_of(block, "body")], factor=2)
    assert moved.error["details"]["check"] == "centroid[0]"  # about [0, 0, 0] it moves


def _rigid(nx, body):
    """A Move Object commit that applies the ToCsys pose to ``body``'s centroid."""
    from nx_mcp.kit import geom

    def moved(b):
        motion = b.TransformMotion
        centre = getattr(body, "centroid", None) or [
            (body.box[i] + body.box[i + 3]) / 2 for i in range(3)
        ]
        if motion.Option is motion.Options.Angle:  # an angle about an axis
            from nx_mcp.kit import pose

            axis = motion.AngularAxis
            origin = geom.xyz(axis.point.Coordinates)
            direction = geom.xyz(axis.direction.Vector)
            angle = float(motion.Angle.RightHandSide)
            pivot, turn = pose.rotate_about(([0.0] * 3, pose.identity()), origin, direction, angle)
            body.centroid = geom.add(geom.matvec(turn, centre), pivot)
        else:
            to, m = motion.ToCsys.args[:2]
            body.centroid = geom.add(geom.matvec(geom.rows(m), centre), [to.X, to.Y, to.Z])
        return feature(nx, "MOVE_OBJECT", [body])

    nx.on_commit("MoveObjectBuilder", moved)


def test_transform_rotates_about_an_axis_through_a_point():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    nx.nx.Features.MoveObject = Object
    _rigid(nx, block)
    spin = {"axis": [0, 0, 1], "angle": 90, "origin": [5, 5, 0]}
    ok = run(nx, "nx_create_transform", bodies=[block.ref], transform={"rotation": spin})
    assert ok.ok, ok
    motion = nx.last("MoveObjectBuilder").TransformMotion
    assert motion.Angle.RightHandSide == "90.0"  # one axis rotation: an angle motion
    assert geom.xyz(motion.AngularAxis.point.Coordinates) == [5.0, 5.0, 0.0]
    assert [round(v, 9) for v in block.centroid] == [5.0, 5.0, 5.0]
    turn_z = {"rotation": {"axis": "z", "angle": "90 deg"}, "translation": [0, 0, 5]}
    turn = run(nx, "nx_create_transform", bodies=[nx.ref_of(block, "body")], transform=turn_z)
    assert turn.ok, turn
    assert [round(v, 9) for v in block.centroid] == [-5.0, 5.0, 10.0]
    move = next(f for f in nx.part.Features if f.FeatureType == "MOVE_OBJECT")
    flip = [[1, 0, 0], [0, -1, 0], [0, 0, -1]]  # nx_edit_feature takes the matrix form
    tilt = {"transform": {"translation": [0, 0, 0], "rotation": flip}}
    edit = run(nx, "nx_edit_feature", feature=nx.ref_of(move, "feature"), definition=tilt)
    assert edit.ok, edit
    still = run(nx, "nx_create_transform", bodies=[nx.ref_of(block, "body")],
                transform={"translation": [0, 0, 0]})  # fmt: skip
    assert still.error["field"] == "/transform" and still.mutation_outcome == "not_started"
    half = {"rotation": {"axis": "z"}}
    bad = run(nx, "nx_create_transform", bodies=[nx.ref_of(block, "body")], transform=half)
    assert bad.error["field"].startswith("/transform/rotation"), bad


def test_transform_copy_is_fixed_at_its_timestamp():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    nx.nx.Features.MoveObject = Object

    def extracted(b):
        return feature(nx, "EXTRACT_BODY", [new_body(nx, [0, 0, 0, 10, 10, 10])])

    nx.on_commit("ExtractFaceBuilder", extracted)
    here = {"translation": [0, 0, 0]}
    in_place = run(nx, "nx_create_transform", bodies=[block.ref], transform=here, copy=True)
    assert in_place.ok and "EXTRACT_BODY" in in_place.data["feature"]["name"], in_place
    assert nx.last("ExtractFaceBuilder").FixAtCurrentTimestamp is True
    assert not any(b.kind == "MoveObjectBuilder" for b in nx.builders)


def rounded_plate(nx):
    """A 40 x 20 x 10 plate with R5 vertical corners: its top loop is 4 lines and 4 arcs
    (one tangent chain); vertical seam edges meet it square at the tangent points."""
    block = nx.block(40, 20, 10)
    z = 10.0
    loop = [  # (start, end, tangent at start, tangent at end)
        ((5, 0), (35, 0), (1, 0), (1, 0)),
        ((35, 0), (40, 5), (1, 0), (0, 1)),
        ((40, 5), (40, 15), (0, 1), (0, 1)),
        ((40, 15), (35, 20), (0, 1), (-1, 0)),
        ((35, 20), (5, 20), (-1, 0), (-1, 0)),
        ((5, 20), (0, 15), (-1, 0), (0, -1)),
        ((0, 15), (0, 5), (0, -1), (0, -1)),
        ((0, 5), (5, 0), (0, -1), (1, 0)),
    ]
    ends = {}
    edges = []
    for s, e, ts, te in loop:
        edge = nx.remember(nx.edge((*s, z), (*e, z), body=block, linear=ts == te))
        ends[int(edge.Tag)] = (((*s, z), (*ts, 0)), ((*e, z), (*te, 0)))
        edges.append(edge)
    for corner in ((5, 0), (35, 0), (40, 5), (40, 15), (35, 20), (5, 20), (0, 15), (0, 5)):
        seam = nx.remember(nx.edge((*corner, 0), (*corner, z), body=block))
        ends[int(seam.Tag)] = (((*corner, 0), (0, 0, 1)), ((*corner, z), (0, 0, 1)))
        edges.append(seam)
    block.edges = edges
    nx.uf.Eval = NS(
        Initialize=lambda tag: tag,
        AskLimits=lambda h: (0.0, 1.0),
        EvaluateUnitVectors=lambda h, t: ends[h][0 if t < 0.5 else 1],
        Free=lambda h: None,
    )
    return block, edges[0:8:2], edges[1:8:2]


@pytest.mark.parametrize("tool", ["nx_create_chamfer", "nx_create_blend"])
def test_part_of_a_tangent_chain_is_noted_or_completed(tool):
    nx = modeling_nx()
    block, lines, arcs = rounded_plate(nx)
    kind = "ChamferBuilder" if tool == "nx_create_chamfer" else "EdgeBlendBuilder"
    size = {"offset": 1} if tool == "nx_create_chamfer" else {"radius": 1}
    changes(nx, kind, block, 7900.0)
    part = run(nx, tool, edges=[e.ref for e in lines], **size)
    assert part.ok, part
    note = [w["message"] for w in part.warnings if w["code"] == "NX_NOTE"]
    assert note and "4 edge(s)" in note[0] and "chain true" in note[0], part.warnings
    changes(nx, kind, block, 7800.0)
    whole = run(nx, tool, edges=[nx.ref_of(e, "edge") for e in lines], chain=True, **size)
    assert whole.ok and not whole.warnings, whole
    b = nx.last(kind)
    collector = b.SmartCollector if kind == "ChamferBuilder" else b.AddChainset.calls[0][0][0]
    ((_kind, chained),) = collector.rules
    assert {int(e.Tag) for e in chained} == {int(e.Tag) for e in lines + arcs}

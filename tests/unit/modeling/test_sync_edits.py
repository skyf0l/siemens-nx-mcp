"""Synchronous edits of nx_edit_faces for imported (dumb) solids: resize faces, blends and
chamfers, pattern and mirror faces, delete blend chains; on the shared fake through the
real kernel."""

import math

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx


def setup(kind="cylinder", radius=5.0):
    """A 40x20x10 block with one extra face of ``kind`` (a hole, cone or blend)."""
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    face = nx.remember(nx.face(normal=(0, 0, 1), origin=(10, 10, 0), kind=kind, body=block))
    face.radius = radius
    block.faces.append(face)
    return nx, block, face


def edits(nx, kind, block, volume, then=None):
    def hook(b):
        block.volume = volume
        if then:
            then(b)
        return feature(nx, kind, [block])

    nx.on_commit(kind, hook)


def answers(nx, kind, **members):
    """Builders of ``kind`` answer these methods (e.g. IsBlendFace)."""
    make = nx.builder

    def builder(name, *args):
        b = make(name, *args)
        if name == kind:
            for key, value in members.items():
                object.__setattr__(b, key, value)
        return b

    nx.builder = builder


def test_resize_hole_diameter_reads_back_and_refuses_wrong_faces():
    nx, block, hole = setup()
    edits(nx, "ResizeFaceBuilder", block, 7900.0, lambda b: setattr(hole, "radius", 7.0))
    ok = run(nx, "nx_edit_faces", faces=[hole.ref], action="resize", diameter=14)
    assert ok.ok and ok.data["faces"] == 7, ok
    b = nx.last("ResizeFaceBuilder")
    assert b.Face.Add.calls[0][0][0] == [hole] and b.Diameter.RightHandSide == "14.0"
    edits(nx, "ResizeFaceBuilder", block, 7800.0)  # NX left the radius at 7
    wrong = run(nx, "nx_edit_faces", faces=[nx.ref_of(hole, "face")], action="resize", diameter=16)
    assert wrong.error["details"]["check"] == "diameter[0]"
    assert wrong.mutation_outcome == "rolled_back"
    top = nx.ref_of(block.face((0, 0, 1)), "face")
    plane = run(nx, "nx_edit_faces", faces=[top], action="resize", diameter=8)
    assert plane.error["code"] == "NX_WRONG_GEOMETRY" and plane.error["field"] == "/faces/0"
    both = run(nx, "nx_edit_faces", faces=[top], action="resize", diameter=8, angle=30)
    neither = run(nx, "nx_edit_faces", faces=[top], action="resize")
    assert both.error["field"] == neither.error["field"] == "/diameter"
    assert both.mutation_outcome == "not_started"
    zero = run(nx, "nx_edit_faces", faces=[top], action="resize", diameter=0)
    assert zero.error["field"] == "/diameter"


def test_resize_cone_half_angle():
    nx, block, cone = setup("cone", 4.0)
    plain = nx.uf.Modeling.AskFaceData
    half = {"angle": math.radians(45)}

    def face_data(tag):
        data = list(plain(tag))
        if tag == cone.Tag:
            data[5] = half["angle"]
        return tuple(data)

    nx.uf.Modeling.AskFaceData = face_data
    edits(nx, "ResizeFaceBuilder", block, 7950.0, lambda b: half.update(angle=math.radians(30)))
    ok = run(nx, "nx_edit_faces", faces=[cone.ref], action="resize", angle=30)
    assert ok.ok and nx.last("ResizeFaceBuilder").Angle.RightHandSide == "30.0", ok
    cylinder = run(nx, "nx_edit_faces", faces=[nx.ref_of(cone, "face")], action="resize",
                   diameter=8)  # fmt: skip
    assert cylinder.error["code"] == "NX_WRONG_GEOMETRY"
    flat = run(nx, "nx_edit_faces", faces=[nx.ref_of(cone, "face")], action="resize", angle=90)
    assert flat.error["field"] == "/angle"


def test_resize_blend_needs_a_recognized_blend():
    nx, block, blend = setup(radius=3.0)
    answers(nx, "ResizeBlendBuilder", IsBlendFace=lambda faces: [True] * len(faces))
    edits(nx, "ResizeBlendBuilder", block, 7900.0)
    ok = run(nx, "nx_edit_faces", faces=[blend.ref], action="resize_blend", radius=5)
    assert ok.ok, ok
    b = nx.last("ResizeBlendBuilder")
    assert b.Radius.RightHandSide == "5.0" and b.BlendFace.ReplaceRules.calls
    answers(nx, "ResizeBlendBuilder", IsBlendFace=lambda faces: [False] * len(faces))
    no = run(nx, "nx_edit_faces", faces=[nx.ref_of(blend, "face")], action="resize_blend",
             radius=4)  # fmt: skip
    assert no.error["code"] == "NX_WRONG_GEOMETRY" and no.error["field"] == "/faces/0"
    assert nx.last("ResizeBlendBuilder").destroyed and not nx.last("ResizeBlendBuilder").commits
    missing = run(nx, "nx_edit_faces", faces=[nx.ref_of(blend, "face")], action="resize_blend")
    assert missing.error["field"] == "/radius"


def test_resize_chamfer_symmetric_offset():
    nx, block, _ = setup()
    face = block.face((0, 1, 0))
    edits(nx, "ResizeChamferBuilder", block, 7700.0)
    ok = run(nx, "nx_edit_faces", faces=[face.ref], action="resize_chamfer", distance=4)
    assert ok.ok, ok
    b = nx.last("ResizeChamferBuilder")
    assert b.Offset1.RightHandSide == b.Offset2.RightHandSide == "4.0"
    assert b.ChamferType is nx.nx.Features.ResizeChamferBuilder.SectionType.SymmetricOffset
    bad = run(nx, "nx_edit_faces", faces=[nx.ref_of(face, "face")], action="resize_chamfer",
              distance=-1)  # fmt: skip
    assert bad.error["field"] == "/distance" and bad.mutation_outcome == "not_started"
    other = run(nx, "nx_edit_faces", faces=[nx.ref_of(face, "face")], action="resize_chamfer",
                radius=1)  # fmt: skip
    assert other.error["field"] == "/distance"


def test_pattern_faces_linear_and_circular():
    nx, block, hole = setup()

    def copy(b):
        block.faces.append(nx.face(kind="cylinder", body=block))

    edits(nx, "PatternFaceBuilder", block, 7700.0, copy)
    linear = {"layout": "linear", "count": 3, "spacing": 10}
    ok = run(nx, "nx_edit_faces", faces=[hole.ref], action="pattern", pattern=linear,
             direction={"principal": "x"})  # fmt: skip
    assert ok.ok and ok.data["faces"] == 8, ok
    b = nx.last("PatternFaceBuilder")
    types = nx.nx.Features.PatternFaceBuilder.PatternTypes
    assert b.Type is types.Rectangular and b.XCount.RightHandSide == "3"
    assert b.XDistance.RightHandSide == "10.0" and b.YCount.RightHandSide == "1"
    edits(nx, "PatternFaceBuilder", block, 7500.0, copy)
    circular = {"layout": "circular", "origin": [20, 10, 0], "count": 4, "angle": 90}
    ok = run(nx, "nx_edit_faces", faces=[nx.ref_of(hole, "face")], action="pattern",
             pattern=circular, direction=[0, 0, 1])  # fmt: skip
    assert ok.ok, ok
    b = nx.last("PatternFaceBuilder")
    assert b.Type is types.Circular and b.CircularCount.RightHandSide == "4"
    assert b.Angle.RightHandSide == "90.0" and b.Axis.kind == "axis"
    full = run(nx, "nx_edit_faces", faces=[nx.ref_of(hole, "face")], action="pattern",
               pattern={**circular, "count": 5}, direction=[0, 0, 1])  # fmt: skip
    assert full.error["field"] == "/pattern/angle"
    mixed = run(nx, "nx_edit_faces", faces=[nx.ref_of(hole, "face")], action="pattern",
                pattern={**linear, "angle": 5}, direction=[1, 0, 0])  # fmt: skip
    assert mixed.error["field"] == "/pattern/angle" and mixed.mutation_outcome == "not_started"
    nodir = run(nx, "nx_edit_faces", faces=[nx.ref_of(hole, "face")], action="pattern",
                pattern=linear)  # fmt: skip
    assert nodir.error["field"] == "/direction"
    edits(nx, "PatternFaceBuilder", block, 7400.0)  # no new faces
    none = run(nx, "nx_edit_faces", faces=[nx.ref_of(hole, "face")], action="pattern",
               pattern=linear, direction=[1, 0, 0])  # fmt: skip
    assert none.error["details"]["check"] == "faces" and none.mutation_outcome == "rolled_back"


def test_mirror_faces_about_a_plane():
    nx, block, hole = setup()
    edits(nx, "MirrorFaceBuilder", block, 7700.0,
          lambda b: block.faces.append(nx.face(kind="cylinder", body=block)))  # fmt: skip
    plane = {"origin": [20, 0, 0], "normal": [1, 0, 0]}
    ok = run(nx, "nx_edit_faces", faces=[hole.ref], action="mirror", plane=plane)
    assert ok.ok, ok
    b = nx.last("MirrorFaceBuilder")
    assert b.MirrorPlaneOption is nx.nx.Features.MirrorFaceBuilder.MirrorPlaneOptions.New
    assert b.FaceToMirror.FaceCollector.ReplaceRules.calls and b.NewMirrorPlane is not None
    no = run(nx, "nx_edit_faces", faces=[nx.ref_of(hole, "face")], action="mirror")
    assert no.error["field"] == "/plane"


def test_delete_blend_chain_heals():
    nx, block, blend = setup(radius=3.0)
    edits(nx, "DeleteFaceBuilder", block, 8050.0)
    ok = run(nx, "nx_edit_faces", faces=[blend.ref], action="delete_blend")
    assert ok.ok, ok
    b = nx.last("DeleteFaceBuilder")
    assert b.Type is nx.nx.Features.DeleteFaceBuilder.SelectTypes.Blend
    assert b.BlendCollector.ReplaceRules.calls and b.Heal is True
    extra = run(nx, "nx_edit_faces", faces=[nx.ref_of(blend, "face")], action="delete_blend",
                radius=2)  # fmt: skip
    assert extra.error["field"] == "/radius"


def test_resize_takes_a_radius_and_errors_name_what_each_action_takes():
    nx, block, hole = setup()
    edits(nx, "ResizeFaceBuilder", block, 7900.0, lambda b: setattr(hole, "radius", 6.0))
    ok = run(nx, "nx_edit_faces", faces=[hole.ref], action="resize", radius="6 mm")
    assert ok.ok and nx.last("ResizeFaceBuilder").Diameter.RightHandSide == "12.0", ok
    assert ok.data["volume_change"] == ok.data["volume"] - 8000.0
    top = nx.ref_of(block.face((0, 0, 1)), "face")
    none = run(nx, "nx_edit_faces", faces=[top], action="resize")
    assert none.error["field"] == "/diameter" and "radius" in none.error["message"]
    assert "diameter: '12 mm'" in none.error["hint"]
    two = run(nx, "nx_edit_faces", faces=[top], action="resize", diameter=8, radius=4)
    assert two.error["field"] == "/diameter" and two.mutation_outcome == "not_started"
    extra = run(nx, "nx_edit_faces", faces=[top], action="offset", distance=1, direction="z")
    assert extra.error["field"] == "/direction" and "offset takes distance" in extra.error["hint"]


def test_move_and_mirror_take_axis_and_plane_words():
    nx, block, hole = setup()
    edits(nx, "AdmMoveFaceBuilder", block, 7950.0)
    ok = run(nx, "nx_edit_faces", faces=[hole.ref], action="move", distance=3, direction="-z")
    assert ok.ok and ok.data["volume_change"] == -50.0, ok
    motion = nx.last("AdmMoveFaceBuilder").Motion
    assert motion.DistanceValue.RightHandSide == "3.0" and motion.DistanceVector.Vector.Z == -1.0
    edits(nx, "MirrorFaceBuilder", block, 7700.0,
          lambda b: block.faces.append(nx.face(kind="cylinder", body=block)))  # fmt: skip
    mirror = run(nx, "nx_edit_faces", faces=[nx.ref_of(hole, "face")], action="mirror",
                 plane="yz")  # fmt: skip
    assert mirror.ok, mirror
    wrong = run(nx, "nx_edit_faces", faces=[nx.ref_of(hole, "face")], action="mirror",
                plane="zz")  # fmt: skip
    assert wrong.error["field"] == "/plane" and wrong.mutation_outcome == "not_started"

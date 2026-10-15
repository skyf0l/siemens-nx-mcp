"""nx_create_face_label (Label Chamfer / Group Face) and nx_edit_face_region (Offset Region /
Pull Face) on the shared fake through the real kernel."""

import math

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx

R = 1 / math.sqrt(2)


def chamfered():
    """A block whose bottom front edge is chamfered: the chamfer face meets the front and
    bottom faces along 60 long edges and the end faces along 2.83 short ones."""
    nx = modeling_nx()
    a = nx.block(60, 40, 20)
    front, bottom = a.face((0, -1, 0)), a.face((0, 0, -1))
    left, right = a.face((-1, 0, 0)), a.face((1, 0, 0))
    ch = nx.remember(nx.face(normal=(0, -R, -R), origin=(30, 1, 1), body=a))
    a.faces.append(ch)
    edges = []
    for other, size in ((front, 60.0), (left, 2.83), (bottom, 60.0), (right, 2.83)):
        e = nx.remember(nx.edge((0, 0, 0), (size, 0, 0), body=a))
        e.GetFaces = lambda other=other: [ch, other]
        e.GetLength = lambda size=size: size
        edges.append(e)
    ch.GetEdges = lambda: edges
    return nx, a, ch, front, bottom


def test_label_chamfer_finds_its_construction_faces():
    nx, a, ch, front, bottom = chamfered()
    nx.on_commit("LabelChamferBuilder", lambda b: feature(nx, "Label Chamfer", [a]))
    ok = run(nx, "nx_create_face_label", faces=[nx.ref_of(ch, "face")], kind="chamfer")
    assert ok.ok and [r["id"] for r in ok.data["faces"]] == [
        nx.ref_of(front, "face"),
        nx.ref_of(bottom, "face"),
    ], ok
    b = nx.last("LabelChamferBuilder")
    assert b.ChamferFace.ReplaceRules.calls[0][0][0] == [("FaceDumb", [ch])]
    assert b.ConstructionFace1.ReplaceRules.calls[0][0][0] == [("FaceDumb", [front])]
    assert b.ConstructionFace2.ReplaceRules.calls[0][0][0] == [("FaceDumb", [bottom])]
    given = [nx.ref_of(bottom, "face"), nx.ref_of(front, "face")]
    ok = run(nx, "nx_create_face_label", faces=[nx.ref_of(ch, "face")], kind="chamfer",
             between=given)  # fmt: skip
    assert ok.ok and nx.last("LabelChamferBuilder").ConstructionFace1.ReplaceRules.calls[0][0][
        0
    ] == [("FaceDumb", [bottom])]

    def changes(b):
        a.volume -= 1.0
        return feature(nx, "Label Chamfer", [a])

    nx.on_commit("LabelChamferBuilder", changes)
    moved = run(nx, "nx_create_face_label", faces=[nx.ref_of(ch, "face")], kind="chamfer")
    assert moved.error["details"]["check"] == "volume" and moved.mutation_outcome == "rolled_back"


def test_group_and_label_refusals():
    nx, a, ch, front, bottom = chamfered()
    nx.on_commit("GroupFaceBuilder", lambda b: feature(nx, "Group Face", [a]))
    faces = [nx.ref_of(front, "face"), nx.ref_of(bottom, "face")]
    ok = run(nx, "nx_create_face_label", faces=faces, kind="group", rigid=True)
    assert ok.ok and ok.data["faces"] == [], ok
    b = nx.last("GroupFaceBuilder")
    assert b.Rigid is True and b.FaceToGroup.FaceCollector.ReplaceRules.calls[0][0][0] == [
        ("FaceDumb", [front, bottom])
    ]
    one = run(nx, "nx_create_face_label", faces=faces[:1], kind="group")
    two = run(nx, "nx_create_face_label", faces=faces, kind="chamfer")
    assert one.error["field"] == two.error["field"] == "/faces"
    rigid = run(nx, "nx_create_face_label", faces=[nx.ref_of(ch, "face")], kind="chamfer",
                rigid=True)  # fmt: skip
    between = run(nx, "nx_create_face_label", faces=faces, kind="group", between=faces)
    assert rigid.error["field"] == "/rigid" and between.error["field"] == "/between"
    itself = run(nx, "nx_create_face_label", faces=[nx.ref_of(ch, "face")], kind="chamfer",
                 between=[nx.ref_of(ch, "face"), faces[0]])  # fmt: skip
    assert itself.error["field"] == "/between/0" and itself.mutation_outcome == "not_started"


def region(nx, body, face, kind, shift, volume):
    def hook(b):
        old = nx.face_data[face.Tag]
        nx.face_data[face.Tag] = (
            old[0],
            [o + s for o, s in zip(old[1], shift, strict=True)],
            old[2],
        )
        body.volume = volume
        return feature(nx, kind, [body])

    nx.on_commit(kind, hook)


def test_offset_region_and_pull_read_the_face_offsets_back():
    nx = modeling_nx()
    t = nx.block(20, 20, 10)
    top, side = t.face((0, 0, 1)), t.face((1, 0, 0))
    region(nx, t, top, "AdmOffsetRegionBuilder", (0, 0, -4), 2400.0)
    ok = run(nx, "nx_edit_face_region", faces=[nx.ref_of(top, "face")], action="offset",
             distance=-4)  # fmt: skip
    assert ok.ok and ok.data["offsets"] == [-4.0] and ok.data["volume_change"] == -1600.0, ok
    b = nx.last("AdmOffsetRegionBuilder")
    assert b.Distance.RightHandSide == "4.0" and b.ReverseDirection is True
    region(nx, t, side, "PullFaceBuilder", (5, 0, 0), 3400.0)
    ok = run(nx, "nx_edit_face_region", faces=[nx.ref_of(side, "face")], action="pull",
             distance=5, direction="x")  # fmt: skip
    assert ok.ok and ok.data["offsets"] == [5.0], ok
    assert nx.last("PullFaceBuilder").FaceToPull.ReplaceRules.calls[0][0][0] == [
        ("FaceDumb", [side])
    ]
    region(nx, t, side, "PullFaceBuilder", (2, 0, 0), 3000.0)  # NX pulled 2, not 5
    short = run(nx, "nx_edit_face_region", faces=[nx.ref_of(side, "face")], action="pull",
                distance=5, direction="x")  # fmt: skip
    assert (
        short.error["details"]["check"] == "offset[0]" and short.mutation_outcome == "rolled_back"
    )
    no_dir = run(nx, "nx_edit_face_region", faces=[nx.ref_of(side, "face")], action="pull",
                 distance=5)  # fmt: skip
    assert no_dir.error["field"] == "/direction" and no_dir.mutation_outcome == "not_started"


def test_label_between_faces_must_meet_the_chamfer_and_duplicates_warn():
    nx, a, ch, front, bottom = chamfered()
    top = a.face((0, 0, 1))
    bad = run(nx, "nx_create_face_label", faces=[nx.ref_of(ch, "face")], kind="chamfer",
              between=[nx.ref_of(front, "face"), nx.ref_of(top, "face")])  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/between/1", bad
    assert bad.mutation_outcome == "not_started"

    def label(b):
        f = feature(nx, "LABEL_CHAMFER", [a])
        f.GetFaces = lambda: [ch]
        return f

    nx.on_commit("LabelChamferBuilder", label)
    first = run(nx, "nx_create_face_label", faces=[nx.ref_of(ch, "face")], kind="chamfer")
    assert first.ok and not first.warnings, first
    second = run(nx, "nx_create_face_label", faces=[nx.ref_of(ch, "face")], kind="chamfer")
    assert second.ok and any("already labels" in w["message"] for w in second.warnings), second

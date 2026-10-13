"""nx_create_face_copy (Copy/Cut Face) and nx_create_face_paste (Paste Face) on the shared
fake through the real kernel."""

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body


def setup():
    """Block A 60x40x20 with a boss (cylinder + cap) and block T at x=100."""
    nx = modeling_nx()
    a = nx.block(60, 40, 20)
    side = nx.remember(nx.face(normal=(0, 0, 1), origin=(15, 20, 20), kind="cylinder", body=a))
    cap = nx.remember(nx.face(normal=(0, 0, 1), origin=(15, 20, 28), body=a))
    side.box, cap.box = [10, 15, 20, 20, 25, 28], [10, 15, 28, 20, 25, 28]
    a.faces += [side, cap]
    t = nx.block(20, 20, 10, origin=(100.0, 0.0, 0.0))
    for body in (a, t):
        body.contains = lambda p: 1
    return nx, a, t, side, cap


def copy(nx, faces, **args):
    return run(nx, "nx_create_face_copy", faces=[nx.ref_of(f, "face") for f in faces], **args)


def test_copy_pastes_into_the_body_with_a_csys_motion():
    nx, a, _, side, cap = setup()

    def hook(b):
        a.volume += 628.0
        return feature(nx, "COPY_FACE", [a])

    nx.on_commit("CopyFaceBuilder", hook)
    ok = copy(nx, [side, cap], mode="copy", transform={"translation": [20, 0, 0]})
    assert ok.ok and ok.data["volume_change"] == 628.0 and ok.data["sheet"] is None, ok
    b = nx.last("CopyFaceBuilder")
    rules = b.FaceToCopy.FaceCollector.ReplaceRules.calls[0][0][0]
    assert rules == [("FaceDumb", [side, cap])] and b.DoPaste is True and b.destroyed
    to = b.Motion.ToCsys.args[0]
    assert (to.X, to.Y, to.Z) == (20.0, 0.0, 0.0)
    nx.on_commit("CopyFaceBuilder", lambda b: feature(nx, "COPY_FACE", [a]))  # nothing pasted
    same = copy(nx, [side, cap], mode="copy", transform={"translation": [20, 0, 0]})
    assert same.error["details"]["check"] == "volume" and same.mutation_outcome == "rolled_back"


def test_cut_to_a_sheet_reads_its_place_back():
    nx, a, _, side, cap = setup()

    def hook(sheet_box):
        def commit(b):
            a.volume -= 628.0
            sheet = new_body(nx, sheet_box)
            sheet.IsSolidBody = False
            return feature(nx, "CUT_FACE", [sheet])

        return commit

    nx.on_commit("CutFaceBuilder", hook([110, 5, 10, 120, 15, 18]))
    move = {"translation": [100, -10, -10]}
    ok = copy(nx, [side, cap], mode="cut", paste=False, transform=move)
    assert ok.ok and ok.data["sheet"]["kind"] == "body" and ok.data["volume_change"] == -628.0, ok
    assert nx.last("CutFaceBuilder").DoPaste is False
    nx.on_commit("CutFaceBuilder", hook([0, 0, 0, 10, 10, 8]))  # NX put the sheet elsewhere
    off = copy(nx, [side, cap], mode="cut", paste=False, transform=move)
    assert off.error["details"]["check"] == "placed" and off.mutation_outcome == "rolled_back"


def test_copy_refusals_before_nx():
    nx, a, t, side, _ = setup()
    still = copy(nx, [side], mode="copy", transform={"translation": [0, 0, 0]})
    assert still.error["field"] == "/transform" and still.mutation_outcome == "not_started"
    mixed = copy(nx, [side, t.face((0, 0, 1))], mode="copy", transform={"translation": [1, 0, 0]})
    assert mixed.error["field"] == "/faces" and mixed.mutation_outcome == "not_started"
    assert not [b for b in nx.builders if b.kind == "CopyFaceBuilder"]


def paste_setup():
    nx, a, t, side, cap = setup()
    sheet = new_body(nx, [105, 5, 10, 115, 15, 18])
    sheet.IsSolidBody = False
    return nx, t, sheet


def test_paste_adds_and_subtracts_with_the_volume_read_back():
    nx, t, sheet = paste_setup()

    def grows(delta):
        def commit(b):
            t.volume += delta
            return feature(nx, "PASTE_FACE", [t])

        return commit

    nx.on_commit("PasteFaceBuilder", grows(628.0))
    tools = [nx.ref_of(sheet, "body")]
    ok = run(nx, "nx_create_face_paste", target=nx.ref_of(t, "body"), tools=tools, operation="add")
    assert ok.ok and ok.data["volume"] == 4628.0, ok
    b = nx.last("PasteFaceBuilder")
    assert b.TargetBody.Value is t and b.ToolBody.Add.calls[0][0][0] == [sheet]
    assert b.PasteOption is nx.nx.Features.PasteFaceBuilder.PasteOptionType.Add
    nx.on_commit("PasteFaceBuilder", grows(628.0))  # subtract that added material
    wrong = run(nx, "nx_create_face_paste", target=nx.ref_of(t, "body"), tools=tools,
                operation="subtract")  # fmt: skip
    assert wrong.error["details"]["check"] == "volume" and wrong.mutation_outcome == "rolled_back"


def test_paste_subtract_that_keeps_the_inside_is_refused():
    nx, t, sheet = paste_setup()
    t.centroid = [110.0, 10.0, 5.0]
    sheet.box = [100, 0, 8, 104, 4, 10]  # away from the centroid

    def inverted(b):
        t.volume = 100.0
        t.contains = lambda p: 2  # the centroid is no longer inside
        return feature(nx, "PASTE_FACE", [t])

    nx.on_commit("PasteFaceBuilder", inverted)
    bad = run(nx, "nx_create_face_paste", target=nx.ref_of(t, "body"),
              tools=[nx.ref_of(sheet, "body")], operation="subtract")  # fmt: skip
    assert bad.error["details"]["check"] == "kept" and bad.mutation_outcome == "rolled_back"
    same = run(nx, "nx_create_face_paste", target=nx.ref_of(t, "body"),
               tools=[nx.ref_of(t, "body")])  # fmt: skip
    assert same.error["field"] == "/tools/0" and same.mutation_outcome == "not_started"

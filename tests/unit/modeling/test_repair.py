"""Patch, Delete Body, Unsew and Untrim on the shared fake, through the real kernel."""

from types import SimpleNamespace as NS

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body


def _areas(nx):
    """Face areas from each fake face's ``area`` (default 100)."""

    def props(_a, _l, _acc, faces):
        return NS(Area=sum(getattr(f, "area", 100.0) for f in faces), Dispose=lambda: None)

    nx.part.MeasureManager.NewFaceProperties = props


def _sheet(nx, box, faces=1):
    body = new_body(nx, box, volume=0.0)
    body.IsSolidBody, body.IsSheetBody = False, True
    body.faces = [nx.face(body=body) for _ in range(faces)]
    body.ref = nx.ref_of(body, "body")
    return body


# ---- patch -------------------------------------------------------------------------


def _patches(nx, target, sheet, volume):
    def hook(_b):
        target.volume = volume
        nx.part.Bodies.remove(sheet)
        return feature(nx, "PATCH", [target])

    nx.on_commit("PatchBuilder", hook)


def test_patch_configures_target_tool_and_reads_the_volume():
    nx = modeling_nx()
    _areas(nx)
    block = nx.block(40, 30, 20)
    sheet = _sheet(nx, [0, 0, 15, 40, 30, 15])
    _patches(nx, block, sheet, 18000.0)
    ok = run(nx, "nx_create_patch_body", body=block.ref, sheet=sheet.ref, reverse=True, hole=True)
    assert ok.ok, ok
    b = nx.last("PatchBuilder")
    assert b.Target.Value is block and b.Tool.Value is sheet
    assert b.ReverseDirection is True and b.CreateHolePatch is True
    assert b.Tolerance == 0.001 and b.destroyed
    assert ok.data["volume"] == 18000.0 and ok.data["volume_before"] == 24000.0
    assert ok.data["sheet"] is False and len(ok.data["bodies"]) == 1


def test_patch_that_changes_nothing_rolls_back():
    nx = modeling_nx()
    _areas(nx)
    block = nx.block(40, 30, 20)
    sheet = _sheet(nx, [0, 0, 15, 40, 30, 15])
    _patches(nx, block, sheet, 24000.0)
    bad = run(nx, "nx_create_patch_body", body=block.ref, sheet=sheet.ref)
    assert bad.error["details"]["check"] == "volume" and bad.mutation_outcome == "rolled_back"


def test_patch_refusal_gets_the_edge_hint():
    nx = modeling_nx()
    _areas(nx)
    block = nx.block(40, 30, 20)
    sheet = _sheet(nx, [0, 0, 15, 40, 30, 15])
    nx.refuse("PatchBuilder", message="Tool sheet edges are not close to target body")
    bad = run(nx, "nx_create_patch_body", body=block.ref, sheet=sheet.ref)
    assert bad.error["code"] == "NX_BUILD_FAILED" and "lie on the body" in bad.error["hint"]


def test_patch_arguments_are_checked_first():
    nx = modeling_nx()
    block = nx.block(40, 30, 20)
    other = nx.block(10, 10, 10, origin=(50, 0, 0))
    same = run(nx, "nx_create_patch_body", body=block.ref, sheet=block.ref)
    assert same.error["field"] == "/sheet" and same.mutation_outcome == "not_started"
    solid = run(nx, "nx_create_patch_body", body=block.ref, sheet=other.ref)
    assert solid.error["code"] == "NX_WRONG_GEOMETRY" and solid.error["field"] == "/sheet"
    sheet = _sheet(nx, [0, 0, 15, 40, 30, 15])
    tol = run(nx, "nx_create_patch_body", body=block.ref, sheet=sheet.ref, tolerance=0)
    assert tol.error["field"] == "/tolerance" and tol.mutation_outcome == "not_started"


# ---- delete body ---------------------------------------------------------------------


def test_delete_body_removes_the_bodies_as_a_feature():
    nx = modeling_nx()
    keep = nx.block(10, 10, 10)
    gone = nx.block(10, 10, 10, origin=(20, 0, 0))

    def hook(b):
        nx.part.Bodies.remove(gone)
        return feature(nx, "Delete Body", [gone])

    nx.on_commit("DeleteBodyBuilder", hook)
    ok = run(nx, "nx_create_delete_body", bodies=[gone.ref])
    assert ok.ok, ok
    b = nx.last("DeleteBodyBuilder")
    assert b.BodyToDelete.GetRules()[0][1] == [gone]
    assert [r["id"] for r in ok.data["deleted"]] == [gone.ref] and ok.data["remaining"] == 1
    assert keep in nx.part.Bodies


def test_delete_body_that_keeps_the_body_rolls_back():
    nx = modeling_nx()
    gone = nx.block(10, 10, 10)
    nx.on_commit("DeleteBodyBuilder", lambda b: feature(nx, "Delete Body", [gone]))
    bad = run(nx, "nx_create_delete_body", bodies=[gone.ref])
    assert bad.error["details"]["check"] == "deleted" and bad.mutation_outcome == "rolled_back"


# ---- unsew ---------------------------------------------------------------------------


def _unsews(nx, block, pieces):
    """Commit turns ``block`` into a sheet and adds sheets owning ``pieces`` faces each."""

    def hook(_b):
        block.IsSolidBody, block.IsSheetBody = False, True
        made = []
        for faces in pieces:
            body = _sheet(nx, [0, 0, 0, 1, 1, 1])
            body.faces = faces
            made.append(body)
        block.faces = [f for f in block.faces if all(f not in p for p in pieces)]
        return feature(nx, "UNSEW", made)

    nx.on_commit("UnsewBuilder", hook)


def test_unsew_faces_splits_into_sheets_with_the_same_area():
    nx = modeling_nx()
    _areas(nx)
    block = nx.block(40, 30, 20)
    top, side = block.face((0, 0, 1)), block.face((1, 0, 0))
    _unsews(nx, block, [[top], [side]])
    ok = run(nx, "nx_create_unsew", by="faces", faces=[top.ref, side.ref], output="each_face")
    assert ok.ok, ok
    b = nx.last("UnsewBuilder")
    assert b.FaceCollector.ReplaceRules.calls[0][0][0][0][1] == [top, side]
    assert "OneBodyForEachFace" in str(b.Output) and b.KeepOriginal is False
    assert "UnsewTool.Face" in str(b.ToolOption)
    assert ok.data["count"] == 3 and ok.data["area"] == 600.0


def test_unsew_edges_uses_the_edge_collector():
    nx = modeling_nx()
    _areas(nx)
    block = nx.block(40, 30, 20)
    top = block.face((0, 0, 1))
    tops = [e for e in block.edges if all(v.Z == 20 for v in e.GetVertices())]
    _unsews(nx, block, [[top]])
    ok = run(nx, "nx_create_unsew", by="edges", edges=[e.ref for e in tops])
    assert ok.ok, ok
    b = nx.last("UnsewBuilder")
    assert "UnsewTool.Edge" in str(b.ToolOption)
    assert b.EdgeCollector.ReplaceRules.calls[0][0][0][0][1] == tops
    assert "OneBodyForConnectedFaces" in str(b.Output) and ok.data["count"] == 2


def test_unsew_that_loses_faces_rolls_back():
    nx = modeling_nx()
    _areas(nx)
    block = nx.block(40, 30, 20)
    top = block.face((0, 0, 1))

    def hook(_b):
        block.IsSolidBody, block.IsSheetBody = False, True
        block.faces = block.faces[2:]  # NX lost a face
        return feature(nx, "UNSEW", [_sheet(nx, [0, 0, 0, 1, 1, 1])])

    nx.on_commit("UnsewBuilder", hook)
    bad = run(nx, "nx_create_unsew", faces=[top.ref])
    assert bad.error["details"]["check"] == "area" and bad.mutation_outcome == "rolled_back"


def test_unsew_needs_the_variant_field_and_one_body():
    nx = modeling_nx()
    a = nx.block(10, 10, 10)
    b = nx.block(10, 10, 10, origin=(20, 0, 0))
    missing = run(nx, "nx_create_unsew", by="edges", faces=[a.faces[0].ref])
    assert missing.mutation_outcome == "not_started" and "edges" in str(missing.error)
    two = run(nx, "nx_create_unsew", faces=[a.faces[0].ref, b.faces[0].ref])
    assert two.error["field"] == "/faces" and two.mutation_outcome == "not_started"


# ---- untrim --------------------------------------------------------------------------


def test_untrim_makes_a_larger_sheet_per_face():
    nx = modeling_nx()
    _areas(nx)
    block = nx.block(40, 30, 20)
    top = block.face((0, 0, 1))
    top.area = 1121.46

    def hook(_b):
        sheet = _sheet(nx, [-20, -15, 20, 60, 45, 20])
        sheet.faces[0].area = 4800.0
        return feature(nx, "UNTRIM", [sheet])

    nx.on_commit("UntrimBuilder", hook)
    ok = run(nx, "nx_create_untrim", faces=[top.ref], hide_original=True)
    assert ok.ok, ok
    b = nx.last("UntrimBuilder")
    assert b.FacesToUntrim.ReplaceRules.calls[0][0][0][0][1] == [top]
    assert b.Associative is True and b.HideOriginal is True
    assert ok.data["area"] == 4800.0 and ok.data["trimmed_area"] == 1121.46
    assert len(ok.data["bodies"]) == 1


def test_untrim_smaller_than_the_face_rolls_back():
    nx = modeling_nx()
    _areas(nx)
    block = nx.block(40, 30, 20)
    top = block.face((0, 0, 1))
    top.area = 1200.0

    def hook(_b):
        sheet = _sheet(nx, [0, 0, 20, 40, 30, 20])
        sheet.faces[0].area = 600.0
        return feature(nx, "UNTRIM", [sheet])

    nx.on_commit("UntrimBuilder", hook)
    bad = run(nx, "nx_create_untrim", faces=[top.ref])
    assert bad.error["details"]["check"] == "area" and bad.mutation_outcome == "rolled_back"

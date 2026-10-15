"""nx_create_fill_hole and nx_create_refit_face on the shared fake."""

import pytest

from tests.kit import run
from tests.kit.fake import FakeSection
from tests.unit.freeform.fakes import curve, edge, face, flat, freeform_nx, made, sheet

RIM = [[-10, -10, 30], [10, -10, 30], [10, 10, 30], [-10, 10, 30]]
BSURFACE = 43


def tube_nx():
    """A square tube sheet whose top rim edges have one face each; a sewn fill adds a
    face and closes the rim edges, an unsewn one makes a separate 400 mm^2 sheet."""
    nx = freeform_nx()
    nx.licenses["solid_modeling"] = True
    nx.part.Features.FreeformSurfaceCollection = nx.part.Features
    walls = [face(nx, flat(), area=600.0) for _ in range(4)]
    rim = [edge(nx, RIM[i], RIM[(i + 1) % 4], [walls[i]]) for i in range(4)]
    tube = sheet(nx, (-10, -10, 0, 10, 10, 30), walls, rim)
    make = nx.builder

    def builder(kind, *args):
        b = make(kind, *args)
        if kind == "FillHoleBuilder":
            object.__setattr__(b, "CurveChain", FakeSection())
        return b

    nx.builder = builder

    def commit(b):
        cap = face(nx, flat(), area=400.0)
        if b.Patch:
            for e, wall in zip(rim, walls, strict=True):
                e.GetFaces = lambda wall=wall: [wall, cap]
            tube.faces.append(cap)
            return made(nx, "FILL_HOLE", [tube])
        own = [edge(nx, RIM[i], RIM[(i + 1) % 4], [cap]) for i in range(4)]
        return made(nx, "FILL_HOLE", [sheet(nx, (-10, -10, 30, 10, 10, 30), [cap], own)])

    nx.on_commit("FillHoleBuilder", commit)
    return nx, tube, rim


def test_fill_hole_sews_a_g0_patch_into_the_sheet():
    nx, tube, rim = tube_nx()
    ok = run(nx, "nx_create_fill_hole", boundary=[e.ref for e in rim[::-1]])
    assert ok.ok, ok
    b = nx.last("FillHoleBuilder")
    H = nx.nx.Features.FillHoleBuilder
    assert b.Patch is True and b.Tolerance == pytest.approx(0.001)
    assert b.DefaultEdgeContinuity is H.ContinuityTypes.G0
    assert b.CurveChain.rules == [("EdgeDumb", rim[::-1])]
    items = b.SetBorderTypeItems.calls[0][0][0]
    assert [i.BorderObject for i in items] == rim[::-1]
    assert all(i.Continuity is H.ContinuityTypes.G0 for i in items)
    assert nx.license_queries == ["solid_modeling", "nx_freeform_1"]
    assert ok.data["sewn"] is True and ok.data["faces"] == 5
    assert ok.data["free_edges"] == 0 and ok.data["closed"] is True
    assert ok.data["bodies"][0]["id"] == tube.ref and nx.live_builders == 0


def test_fill_hole_g1_separate_sheet():
    nx, _tube, rim = tube_nx()
    ok = run(nx, "nx_create_fill_hole", boundary=[e.ref for e in rim], continuity="g1",
             sew=False)  # fmt: skip
    assert ok.ok, ok
    b = nx.last("FillHoleBuilder")
    g1 = nx.nx.Features.FillHoleBuilder.ContinuityTypes.G1
    assert b.Patch is False and b.DefaultEdgeContinuity is g1
    assert ok.data["sewn"] is False and ok.data["free_edges"] == 4


def test_fill_hole_unsewn_patch_fails_read_back():
    nx, _tube, rim = tube_nx()
    nx.on_commit("FillHoleBuilder", lambda b: made(nx, "FILL_HOLE", [_tube]))  # nothing sewn
    bad = run(nx, "nx_create_fill_hole", boundary=[e.ref for e in rim])
    assert bad.error["details"]["check"] == "sewn", bad
    assert bad.mutation_outcome == "rolled_back"


def test_fill_hole_refusals_before_nx():
    nx, _tube, rim = tube_nx()
    loose = [curve(nx, RIM[i], RIM[(i + 1) % 4]) for i in range(4)]
    bad = run(nx, "nx_create_fill_hole", boundary=[c.ref for c in loose])
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/boundary/0"
    bad = run(nx, "nx_create_fill_hole", boundary=[e.ref for e in rim[:3]])
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/boundary"
    assert not nx.builders
    ok = run(nx, "nx_create_fill_hole", boundary=[c.ref for c in loose], sew=False)
    assert ok.ok, ok  # curves can be filled as a separate G0 sheet


def test_fill_hole_without_license_refuses_before_the_builder():
    nx, _tube, rim = tube_nx()
    nx.licenses["nx_freeform_1"] = False
    bad = run(nx, "nx_create_fill_hole", boundary=[e.ref for e in rim])
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and not nx.builders


# ---------------------------------------------------------------------------
# Refit face


def refit_nx(kind=BSURFACE, shift=0.0):
    nx = freeform_nx()
    nx.licenses["solid_modeling"] = True
    f = face(nx, flat(), area=1000.0)
    f.at = lambda u, v: [50.0 * u, 20.0 * v, 0.0]
    f.box = [0, 0, 0, 50, 20, 0]
    f.GetEdges = lambda: list(body.edges)
    body = sheet(nx, (0, 0, 0, 50, 20, 0), [f], [edge(nx, [0, 0, 0], [50, 0, 0], [f])])
    plain = nx.uf.Modeling.AskFaceData
    nx.uf.Modeling.AskFaceData = lambda tag: (
        (kind, [0, 0, 0], [0, 0, 1], [0] * 6, 0.0, 0.0, 1) if tag == f.Tag else plain(tag)
    )

    def commit(_b):
        f.box = [0, 0, shift, 50, 20, shift]
        return made(nx, "REFIT_SURFACE", [body])

    nx.on_commit("RefitFaceBuilder", commit)
    return nx, f


def test_refit_face_within_tolerance():
    nx, f = refit_nx()
    ok = run(nx, "nx_create_refit_face", face=f.ref, tolerance=0.02)
    assert ok.ok, ok
    b = nx.last("RefitFaceBuilder")
    assert b.Face.Value is f and b.RefitControl.Tolerance == pytest.approx(0.02)
    assert ok.data["deviation"] == pytest.approx(0) and ok.data["samples"] == 26
    assert nx.license_queries == ["solid_modeling"] and nx.live_builders == 0


def test_refit_face_out_of_tolerance_rolls_back():
    nx, f = refit_nx(shift=0.5)
    bad = run(nx, "nx_create_refit_face", face=f.ref)
    assert bad.error["details"]["check"] == "deviation", bad
    assert bad.mutation_outcome == "rolled_back"


@pytest.mark.parametrize(("kind", "args", "field"), [
    (22, {}, "/face"),
    (BSURFACE, {"tolerance": 0}, "/tolerance"),
])  # fmt: skip
def test_refit_face_refusals_before_nx(kind, args, field):
    nx, f = refit_nx(kind=kind)
    bad = run(nx, "nx_create_refit_face", face=f.ref, **args)
    assert bad.error["field"] == field, bad
    assert not nx.builders


def test_refit_face_reports_an_unchanged_face_and_a_changed_area():
    nx, f = refit_nx()
    ok = run(nx, "nx_create_refit_face", face=f.ref, tolerance=0.5)
    assert ok.ok, ok.error
    assert ok.data["area_before"] == pytest.approx(1000.0) and ok.data["changed"] is False
    assert [w["code"] for w in ok.warnings] == ["NX_NOTE"]
    assert "before" not in ok.data and "after" not in ok.data

    def smoother(_b):
        f.area = 999.0
        return made(nx, "REFIT_SURFACE", [f.GetBody()])

    nx.on_commit("RefitFaceBuilder", smoother)
    simpler = run(nx, "nx_create_refit_face", face=f.ref, tolerance=0.5)
    assert simpler.ok, simpler.error
    assert simpler.data["area_before"] == pytest.approx(1000.0)
    assert simpler.data["changed"] is True and not simpler.warnings


def test_refit_face_moved_within_tolerance_is_changed():
    nx, f = refit_nx(shift=0.005)
    ok = run(nx, "nx_create_refit_face", face=f.ref, tolerance=0.01)
    assert ok.ok, ok.error
    assert ok.data["deviation"] == pytest.approx(0.005) and ok.data["changed"] is True

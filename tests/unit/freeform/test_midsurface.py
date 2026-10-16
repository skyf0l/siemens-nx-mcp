"""nx_create_midsurface on the shared fake."""

import pytest

from tests.kit import run
from tests.unit.freeform.fakes import face, flat, made, nx_refuses, sheet
from tests.unit.freeform.support import curves_nx

KINDS = {
    "auto": "MidSurfaceBuilder",
    "face_pairs": "MidSurfaceByFacePairsBuilder",
    "two_sheets": "MidSurfaceByTwoSheetsBuilder",
}


def _sampler(box):
    """``at(u, v)`` over a flat axis-aligned face box [x0, y0, z0, x1, y1, z1]."""
    free = [k for k in range(3) if box[k] != box[k + 3]]

    def at(u, v):
        p = [float(box[k]) for k in range(3)]
        for k, t in zip(free, (u, v), strict=False):
            p[k] = box[k] + t * (box[k + 3] - box[k])
        return p

    return at


def mid_nx(z=2.0):
    """A 40 x 20 x 4 plate at y 50; every midsurface commit builds a sheet at ``z``."""
    nx = curves_nx()
    nx.licenses["solid_modeling"] = True
    rules = nx.part.ScRuleFactory
    rules.CreateRuleBodyDumb = lambda objs, *_: ("BodyDumb", list(objs))
    plate = nx.block(40, 20, 4, origin=(0, 50, 0))
    for f in plate.faces:  # interior samples of the plate's faces (the facing pre-check)
        f.at = _sampler(f.box)

    def commit(_b):
        f = face(nx, flat(), area=800.0)
        f.at = lambda u, v: [40.0 * u, 50.0 + 20.0 * v, z]
        return made(nx, "MIDSURFACE", [sheet(nx, (0, 50, z, 40, 70, z), [f])])

    for kind in KINDS.values():
        nx.on_commit(kind, commit)
    return nx, plate


def test_midsurface_auto_pairs_the_plate():
    nx, plate = mid_nx()
    ok = run(nx, "nx_create_midsurface", body=nx.ref_of(plate, "body"))
    assert ok.ok, ok
    b = nx.last("MidSurfaceBuilder")
    rules = b.FacePairing.InputSolidBodies.ReplaceRules.calls[0][0][0]
    assert rules == [("BodyDumb", [plate])]
    assert b.DistanceTolerance == pytest.approx(0.001)
    assert ok.data["method"] == "auto" and ok.data["sheet"] is True
    assert ok.data["half_thickness"] == {"min": pytest.approx(2), "max": pytest.approx(2)}
    assert ok.data["asymmetry"] is None and ok.data["samples"] == 9
    assert nx.license_queries == [] and nx.live_builders == 0


def test_midsurface_face_pairs_is_equidistant():
    nx, plate = mid_nx()
    top, bottom = plate.face((0, 0, 1)), plate.face((0, 0, -1))
    ok = run(nx, "nx_create_midsurface", method="face_pairs", body=nx.ref_of(plate, "body"),
             side_one=[nx.ref_of(top, "face")], side_two=[nx.ref_of(bottom, "face")])  # fmt: skip
    assert ok.ok, ok
    b = nx.last("MidSurfaceByFacePairsBuilder")
    assert b.BodySelection.Add.calls[0][0] == (plate,)
    assert b.SideOneSelection.ReplaceRules.calls[0][0][0] == [("FaceDumb", [top])]
    assert b.CreateFacePair.calls
    assert ok.data["asymmetry"] == pytest.approx(0)


def test_midsurface_leaning_sheet_warns():
    nx, plate = mid_nx(z=1.0)
    top, bottom = plate.face((0, 0, 1)), plate.face((0, 0, -1))
    ok = run(nx, "nx_create_midsurface", method="face_pairs", body=nx.ref_of(plate, "body"),
             side_one=[nx.ref_of(top, "face")], side_two=[nx.ref_of(bottom, "face")])  # fmt: skip
    assert ok.ok and ok.data["asymmetry"] == pytest.approx(2), ok
    assert any(w["code"] == "NX_APPROXIMATED" for w in ok.warnings)


def test_midsurface_between_two_sheets():
    nx, _plate = mid_nx(z=15.0)
    low = sheet(nx, (0, 50, 0, 40, 70, 0), [face(nx, flat())])
    high = sheet(nx, (0, 50, 30, 40, 70, 30), [face(nx, flat())])
    ok = run(nx, "nx_create_midsurface", method="two_sheets", sheets=[low.ref, high.ref])
    assert ok.ok, ok
    b = nx.last("MidSurfaceByTwoSheetsBuilder")
    assert b.FirstSheetBody.ReplaceRules.calls[0][0][0] == [("BodyDumb", [low])]
    assert ok.data["half_thickness"]["max"] == pytest.approx(15)
    assert ok.data["asymmetry"] == pytest.approx(0)


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"method": "face_pairs"}, "/side_one"),
        ({"method": "two_sheets"}, "/sheets"),
        ({"sheets": ["b:1", "b:2"]}, "/sheets"),
    ],
)
def test_midsurface_argument_rules(args, field):
    nx, plate = mid_nx()
    body = {} if args.get("method") == "two_sheets" else {"body": nx.ref_of(plate, "body")}
    bad = run(nx, "nx_create_midsurface", **body, **args)
    assert bad.error["field"].startswith(field), bad  # a stale list ref names its item
    assert not nx.builders


def test_midsurface_refuses_a_sheet_as_the_solid():
    nx, _plate = mid_nx()
    loose = sheet(nx, (0, 0, 0, 1, 1, 0), [face(nx, flat())])
    bad = run(nx, "nx_create_midsurface", body=loose.ref)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/body"


def test_midsurface_without_license_reports_nx_refusal():
    nx, plate = mid_nx()
    nx_refuses(nx, "MidSurfaceBuilder", "solid_modeling")
    bad = run(nx, "nx_create_midsurface", body=nx.ref_of(plate, "body"))
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and bad.error["nx_code"] == 948801
    assert bad.error["details"]["license"] == "solid_modeling"
    assert bad.error["details"]["license_check"] == "missing" and nx.live_builders == 0


def test_midsurface_face_pairs_must_face_each_other():
    nx, plate = mid_nx()
    top, side = plate.face((0, 0, 1)), plate.face((1, 0, 0))
    bad = run(nx, "nx_create_midsurface", method="face_pairs", body=nx.ref_of(plate, "body"),
              side_one=[nx.ref_of(top, "face")], side_two=[nx.ref_of(side, "face")])  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/side_two/0"
    assert bad.error["details"]["angle"] == pytest.approx(90)
    assert bad.mutation_outcome == "not_started" and not nx.builders


def test_midsurface_that_builds_nothing_is_wrong_geometry():
    nx, plate = mid_nx()
    nx.on_commit(KINDS["face_pairs"], lambda _b: made(nx, "MIDSURFACE", []))
    top, bottom = plate.face((0, 0, 1)), plate.face((0, 0, -1))
    bad = run(nx, "nx_create_midsurface", method="face_pairs", body=nx.ref_of(plate, "body"),
              side_one=[nx.ref_of(top, "face")], side_two=[nx.ref_of(bottom, "face")])  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["retry"] == "after_fix"
    assert bad.mutation_outcome == "rolled_back"

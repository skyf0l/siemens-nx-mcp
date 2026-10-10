"""nx_check_interference and nx_export_inspection_report on the fake kit."""

import json
import zipfile
from pathlib import Path

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import capture
from tests.kit import run
from tests.kit.kernel import services
from tests.unit.inspection.fakes import assembly, inspection_nx


def _asm(nx):
    return assembly(
        nx,
        ("A", [0, 0, 0, 10, 10, 10]),
        ("B", [15, 0, 0, 25, 10, 10]),  # 5 from A
        ("C", [5, 5, 5, 15, 15, 15]),  # overlaps A by 125
        ("D", [10, 0, 0, 12, 10, 10]),  # touches A
    )


def test_assembly_pairs_classified_with_overlap_volume_and_rollback():
    nx = inspection_nx()
    _asm(nx)
    bodies_before = list(nx.part.Bodies)
    ok = run(nx, "nx_check_interference")
    assert ok.ok and ok.mutation_outcome == "not_applicable", ok
    data = ok.data
    assert data["pair_count"] == 6 and data["bodies"] == 4
    counts = data["counts"]
    assert counts["penetration"] == 2 and counts["contact"] == 2
    hit = next(p for p in data["pairs"] if p["classification"] == "penetration")
    assert hit["overlap_volume"] == pytest.approx(125) and hit["distance"] == 0
    assert data["skipped_pairs"] + data["checked_pairs"] == 6
    assert list(nx.part.Bodies) == bodies_before and nx.undo_marks == []


def test_clearance_flags_close_pairs_and_contact_can_be_hidden():
    nx = inspection_nx()
    a, b, _c, _d = _asm(nx)
    refs = [nx.ref_of(x, "component") for x in (a, b)]
    near = run(nx, "nx_check_interference", objects=refs, clearance=6)
    assert near.data["counts"]["below_clearance"] == 1 and near.data["pairs"][0]["distance"] == 5
    far = run(nx, "nx_check_interference", objects=refs, clearance=1)
    assert far.data["pairs"] == [] and far.data["skipped_pairs"] == 1
    assert far.data["checked_pairs"] == 0 and far.data["counts"]["clear"] == 1
    listed = run(nx, "nx_check_interference", objects=refs, include_clear=True)
    assert listed.data["pairs"][0]["classification"] == "clear"
    hidden = run(nx, "nx_check_interference", include_contact=False)
    assert all(p["classification"] != "contact" for p in hidden.data["pairs"])
    assert hidden.data["counts"]["contact"] == 2


def test_max_pairs_counts_only_native_checks():
    nx = inspection_nx()
    _asm(nx)
    refused = run(nx, "nx_check_interference", max_pairs=1)
    assert refused.error["code"] == "NX_TOO_MANY_OBJECTS" and refused.error["field"] == "/max_pairs"
    assert refused.error["details"]["native_pairs"] == 4
    assert getattr(nx, "interference_checks", 0) == 0
    bad = run(nx, "nx_check_interference", max_pairs=10001)
    assert bad.error["category"] == "invalid_argument"
    negative = run(nx, "nx_check_interference", clearance=-1)
    assert negative.error["field"] == "/clearance"


def test_same_body_twice_is_invalid():
    nx = inspection_nx()
    body = nx.block(1, 1, 1)
    bad = run(nx, "nx_check_interference", objects=[body.ref, body.ref])
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/objects"


def test_failed_rollback_keeps_the_original_error(monkeypatch):
    nx = inspection_nx()
    a = nx.block(10, 10, 10)
    b = nx.block(10, 10, 10, origin=(5, 0, 0))

    def broken(*_args):
        raise RuntimeError("PerformCheck exploded")

    from tests.unit.inspection import fakes

    monkeypatch.setattr(fakes.Interference, "PerformCheck", broken)
    kit = services(nx)
    monkeypatch.setattr(kit, "undo_to", lambda mark: (_ for _ in ()).throw(RuntimeError("no undo")))
    bad = run(nx, "nx_check_interference", objects=[a.ref, b.ref])
    assert bad.error["code"] == "NX_ROLLBACK_FAILED" and bad.mutation_outcome == "partial"
    original = bad.error["details"]["original_error"]
    assert original["code"] == "NX_ERROR" and "exploded" in original["message"]


def test_report_zip_has_manifest_captures_and_restores_view(monkeypatch, tmp_path):
    nx = inspection_nx()
    _asm(nx)
    shots = []

    def fake_png(tx, path, width, height, **_):
        Path(path).write_bytes(b"\x89PNG fake " + Path(path).name.encode())
        shots.append((Path(path).name, nx.camera.get("scale")))
        return width, height

    monkeypatch.setattr(capture, "studio_png", fake_png)
    nx.camera["scale"] = 3.0
    root = services(nx).root
    ok = run(
        nx, "nx_export_inspection_report", path=str(root / "reports" / "r.zip"),
        section_planes=[{"origin": [5, 5, 5], "normal": [0, 0, 1]}],
    )  # fmt: skip
    assert ok.ok, ok
    data = ok.data
    assert data["path"] == "reports/r.zip" and data["captures"] == 6
    names = [s[0] for s in shots]
    assert names == ["overview.png", *[f"pair-{i}.png" for i in range(1, 5)], "section-1.png"]
    assert nx.part.DynamicSections == []
    with zipfile.ZipFile(root / "reports" / "r.zip") as archive:
        listed = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        report = json.loads(archive.read("report.json"))
    assert {"report.json", "index.html", "manifest.json", "overview.png"} <= listed
    import hashlib

    with zipfile.ZipFile(root / "reports" / "r.zip") as archive:
        for name, digest in manifest.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
    assert report["interference"]["counts"]["penetration"] == 2 and report["units"] == "mm"
    assert nx.camera["scale"] == 3.0 and nx.highlighted == []
    assert not any(getattr(b, "highlighted", False) for b in nx.objects.values())
    assert all(not b.IsBlanked for b in nx.part.Bodies)
    exists = run(nx, "nx_export_inspection_report", path=str(root / "reports" / "r.zip"))
    assert exists.error["category"] in ("already_exists", "invalid_argument")


def test_report_argument_rules_change_nothing(monkeypatch):
    nx = inspection_nx()
    _asm(nx)
    root = services(nx).root
    planes = run(
        nx, "nx_export_inspection_report", path=str(root / "x.zip"), capture=False,
        section_planes=[{"origin": [0, 0, 0], "normal": [0, 0, 1]}],
    )  # fmt: skip
    assert planes.error["field"] == "/section_planes" and planes.mutation_outcome == "not_started"
    txt = run(nx, "nx_export_inspection_report", path=str(root / "x.txt"))
    assert txt.error["category"] in ("invalid_argument", "unsupported", "path_rejected")
    zero = run(
        nx, "nx_export_inspection_report", path=str(root / "y.zip"),
        section_planes=[{"origin": [0, 0, 0], "normal": [0, 0, 0]}],
    )  # fmt: skip
    assert zero.error["field"] == "/section_planes/0/normal"
    assert not (root / "x.zip").exists() and not (root / "y.zip").exists()


def test_report_without_capture(monkeypatch):
    nx = inspection_nx()
    _asm(nx)
    monkeypatch.setattr(
        capture, "studio_png", lambda *a, **k: (_ for _ in ()).throw(NXError("NX_ERROR", "no"))
    )
    root = services(nx).root
    ok = run(nx, "nx_export_inspection_report", path=str(root / "plain.zip"), capture=False)
    assert ok.ok and ok.data["captures"] == 0, ok
    assert ok.data["files"] == ["report.json", "index.html", "manifest.json"]


def _sheet(nx, box):
    body = nx.block(box[3] - box[0], box[4] - box[1], box[5] - box[2], origin=tuple(box[:3]))
    body.IsSolidBody = False
    return body


def test_whole_model_check_skips_sheet_bodies_and_lists_them():
    nx = inspection_nx()
    nx.block(10, 10, 10)
    nx.block(10, 10, 10, origin=(5, 0, 0))
    sheet = _sheet(nx, [0, 0, 0, 10, 10, 10])
    ok = run(nx, "nx_check_interference")
    assert ok.ok, ok
    assert ok.data["bodies"] == 2 and ok.data["pair_count"] == 1
    assert [r["id"] for r in ok.data["skipped_sheet_bodies"]] == [sheet.ref]
    hit = ok.data["pairs"][0]
    assert hit["classification"] == "penetration" and hit["overlap_volume"] == pytest.approx(500)
    assert hit["overlap_box"] == {"min": [5, 0, 0], "max": [10, 10, 10]}


def test_a_sheet_object_is_named_in_the_error():
    nx = inspection_nx()
    solid = nx.block(10, 10, 10)
    sheet = _sheet(nx, [20, 0, 0, 30, 10, 10])
    bad = run(nx, "nx_check_interference", objects=[solid.ref, sheet.ref])
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/objects/1"
    assert sheet.ref in bad.error["message"]
    assert bad.error["details"]["sheet_bodies"][0]["id"] == sheet.ref


def test_only_sheet_bodies_is_wrong_geometry():
    nx = inspection_nx()
    _sheet(nx, [0, 0, 0, 10, 10, 10])
    _sheet(nx, [20, 0, 0, 30, 10, 10])
    bad = run(nx, "nx_check_interference")
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and "2 sheet" in bad.error["message"]
    assert len(bad.error["details"]["sheet_bodies"]) == 2


def test_report_on_a_model_with_sheets_checks_the_solids(monkeypatch):
    nx = inspection_nx()
    nx.block(10, 10, 10)
    nx.block(10, 10, 10, origin=(5, 0, 0))
    _sheet(nx, [0, 0, 0, 10, 10, 10])
    root = services(nx).root
    ok = run(nx, "nx_export_inspection_report", path=str(root / "s.zip"), capture=False)
    assert ok.ok, ok
    assert ok.data["counts"]["penetration"] == 1
    with zipfile.ZipFile(root / "s.zip") as archive:
        report = json.loads(archive.read("report.json"))
    assert len(report["interference"]["skipped_sheet_bodies"]) == 1

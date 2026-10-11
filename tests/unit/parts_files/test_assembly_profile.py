import hashlib
import json
import math
import zipfile
from pathlib import Path

import pytest

from nx_mcp.domains.parts_files import assembly, profile
from tests.kit import NXException
from tests.unit.parts_files.support import call


def saved_assembly(nx):
    blk = nx.new_part("proj/parts/blk.prt")
    blk.Save()
    plate = nx.new_part("proj/parts/plate.prt")
    plate.Save()
    asm = nx.new_part("proj/asm.prt")
    nx.attach(asm, blk)
    nx.attach(asm, blk, "BLK2")
    nx.attach(asm, plate)
    asm.Save()
    return asm, blk, plate


def test_zip_package_has_every_file_and_a_checksum_manifest(nx, root):
    asm, _blk, _plate = saved_assembly(nx)
    ok = call(nx, assembly.nx_export_assembly, part="asm", path="pkg/asm.zip", format="zip")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert ok.data["file_count"] == 3 and ok.data["component_count"] == 3
    with zipfile.ZipFile(root / "pkg" / "asm.zip") as archive:
        manifest = json.loads(archive.read(assembly.MANIFEST))
        for entry in manifest["files"]:
            assert hashlib.sha256(archive.read(entry["path"])).hexdigest() == entry["sha256"]
    assert manifest["assembly"] == "proj/asm.prt"
    assert {e["path"] for e in manifest["files"]} == {
        "proj/asm.prt", "proj/parts/blk.prt", "proj/parts/plate.prt"
    }  # fmt: skip
    assert ok.data["sha256"] == hashlib.sha256((root / "pkg/asm.zip").read_bytes()).hexdigest()


def test_zip_package_rejections(nx, root):
    asm, blk, _plate = saved_assembly(nx)
    (root / "pkg.zip").write_bytes(b"x")
    exists = call(nx, assembly.nx_export_assembly, part="asm", path="pkg.zip", format="zip")
    assert exists.error["code"] == "NX_ALREADY_EXISTS" and exists.mutation_outcome == "not_started"
    suffix = call(nx, assembly.nx_export_assembly, part="asm", path="pkg.tar", format="zip")
    assert suffix.error["code"] == "NX_UNSUPPORTED_FILE_TYPE"
    prefix = call(
        nx, assembly.nx_export_assembly, part="asm", path="p.zip", format="zip", prefix="X_"
    )
    assert prefix.error["field"] == "/prefix"
    blk.IsModified = True
    unsaved = call(nx, assembly.nx_export_assembly, part="asm", path="p.zip", format="zip")
    assert unsaved.error["code"] == "NX_UNSAVED_PART" and unsaved.mutation_outcome == "not_started"
    assert unsaved.error["details"]["part"] == "proj/parts/blk.prt"
    blk.IsModified = False
    asm.ComponentAssembly.RootComponent.children[0].Prototype = None
    unloaded = call(nx, assembly.nx_export_assembly, part="asm", path="p.zip", format="zip")
    assert unloaded.error["code"] == "NX_NOT_LOADED"
    assert not (root / "p.zip").exists()


def test_clone_renames_every_file_loads_as_saved_and_verifies_references(nx, root):
    asm, _blk, _plate = saved_assembly(nx)
    nx.structure[str((root / "proj/asm.prt").resolve()).casefold()] = [
        str(root / "proj/parts/blk.prt"), str(root / "proj/parts/plate.prt")
    ]  # fmt: skip
    loaded = len(nx.session.Parts)
    ok = call(nx, assembly.nx_export_assembly, part="asm", path="copy", format="clone", prefix="C_")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    for rel in ("copy/C_asm.prt", "copy/parts/C_blk.prt", "copy/parts/C_plate.prt"):
        assert (root / rel).is_file()
    manifest = json.loads((root / "copy" / assembly.CLONE_MANIFEST).read_text())
    assert manifest["references_verified"] and manifest["dependency_count"] == 2
    adds = [c for c in nx.uf.Clone.calls if isinstance(c, tuple)]
    assert adds[0][2] == "as_saved"  # components load as saved during the clone
    assert nx.session.Parts.LoadOptions.ComponentLoadMethod == "search"  # restored
    assert len(nx.session.Parts) == loaded and nx.session.Parts.Work is asm  # copy closed
    kept = call(nx, assembly.nx_export_assembly, part="asm", path="copy2", format="clone", prefix="D_",
                activate=True)  # fmt: skip
    assert kept.ok and Path(nx.session.Parts.Work.FullPath) == root / "copy2" / "D_asm.prt"
    assert kept.data["part"]["kind"] == "part"


def test_clone_of_a_background_assembly_keeps_the_engineers_display(nx, root):
    """Gate 4s: the assembly is a quiet work part under another display. The clone never
    displays it, and activate (the copy left as work part) is refused before copying."""
    asm, _blk, _plate = saved_assembly(nx)
    nx.structure[str((root / "proj/asm.prt").resolve()).casefold()] = [
        str(root / "proj/parts/blk.prt"), str(root / "proj/parts/plate.prt")
    ]  # fmt: skip
    other = nx.new_part("other.prt")  # displayed and work: asm is in the background
    nx.session.strict_work = True  # NX: SetWork only under the display part
    ok = call(nx, assembly.nx_export_assembly, part="asm", path="copy", format="clone", prefix="C_")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert (root / "copy/C_asm.prt").is_file()
    assert ("show", asm) not in nx.session.calls  # the assembly was never displayed
    assert nx.session.Parts.Display is other and nx.session.Parts.Work is other
    kept = call(nx, assembly.nx_export_assembly, part="asm", path="copy2", format="clone",
                prefix="D_", activate=True)  # fmt: skip
    assert kept.error["code"] == "NX_WRONG_PART_CONTEXT" and kept.error["field"] == "/activate"
    assert kept.mutation_outcome == "not_started" and not (root / "copy2").exists()
    assert nx.session.Parts.Display is other and nx.session.Parts.Work is other


def test_clone_rejections(nx, root):
    asm, _blk, _plate = saved_assembly(nx)
    missing = call(nx, assembly.nx_export_assembly, part="asm", path="c", format="clone")
    assert missing.error["field"] == "/prefix"
    bad = call(nx, assembly.nx_export_assembly, part="asm", path="c", format="clone", prefix="a b")
    assert bad.error["code"] == "NX_INVALID_ARGUMENT"
    nx.new_part("other/X_blk.prt")
    nx.session.Parts.Work = nx.session.Parts.Display = asm
    clash = call(nx, assembly.nx_export_assembly, part="asm", path="c", format="clone", prefix="X_")
    assert clash.error["code"] == "NX_NAME_CONFLICT" and clash.mutation_outcome == "not_started"
    assert not (root / "c").exists()


def test_clone_failure_removes_the_copy(nx, root):
    saved_assembly(nx)  # no structure: the reopened copy has no components
    bad = call(nx, assembly.nx_export_assembly, part="asm", path="c", format="clone", prefix="Z_")
    assert bad.error["code"] == "NX_VERIFICATION_FAILED"
    assert bad.error["details"]["check"] == "clone_references"
    assert bad.mutation_outcome == "rolled_back" and not (root / "c").exists()


def sketch_with(nx, curves):
    sk = nx.sketch(origin=(0, 0, 5))
    for curve in curves:
        sk.geometry.append(curve)
    return sk


def test_profile_of_a_sketch_writes_analytic_entities(nx, root):
    sk = sketch_with(nx, [
        nx.line((0, 0, 5), (50, 0, 5)), nx.line((50, 0, 5), (50, 30, 5)),
        nx.arc((25, 15, 5), 4.0),
        nx.arc((0, 30, 5), 10.0, a0=0.0, a1=math.pi / 2),
    ])  # fmt: skip
    hole = sk.geometry[2]
    ok = call(nx, profile.nx_export_profile, source=sk.ref, path="cut/p.dxf",
              layers=[[nx.ref_of(hole, "curve"), "HOLES"]])  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert ok.data["entity_counts"] == {"LINE": 2, "CIRCLE": 1, "ARC": 1}
    text = (root / "cut" / "p.dxf").read_text()
    assert "HOLES" in text and "$INSUNITS" in text
    assert ok.data["frame"]["origin"] == [0.0, 0.0, 5.0]
    assert sorted(nx.freed) == sorted(c.Tag for c in sk.geometry)


def test_profile_converts_inch_parts_to_millimetres(root):
    from tests.kit import fake_nx
    from tests.kit.kernel import services
    from tests.kit.parts import with_parts

    nx = fake_nx(units="in")
    with_parts(nx, services(nx).root)
    sk = sketch_with(nx, [nx.line((0, 0, 0), (2, 0, 0))])
    sk.Origin.Z = 0
    ok = call(nx, profile.nx_export_profile, source=sk.ref, path="in.dxf")
    assert ok.data["source_units"] == "in" and ok.data["units"] == "mm"
    text = (services(nx).root / "in.dxf").read_text().splitlines()
    assert "50.8" in text


def test_profile_of_a_face_uses_the_projected_x_axis_and_custom_frames(nx, root):
    face = nx.face(normal=(0, 0, -1), origin=(0, 0, 0))
    face.GetEdges = lambda: [nx.line((0, 0, 0), (10, 0, 0))]
    ok = call(nx, profile.nx_export_profile, source=face.ref, path="f.dxf")
    assert ok.data["frame"]["x_axis"] == [1.0, 0.0, 0.0]
    assert ok.data["frame"]["y_axis"] == [0.0, -1.0, 0.0]
    flipped = call(nx, profile.nx_export_profile, source=face.ref, path="g.dxf",
                   frame=[[0, 0, 0], [-1, 0, 0], [0, 1, 0]])  # fmt: skip
    assert flipped.ok and flipped.data["frame"]["normal"] == [0.0, 0.0, -1.0]


@pytest.mark.parametrize(
    ("extra", "code", "field"),
    [
        ({"frame": [[0, 0, 0], [1, 0, 0], [0, 0, 1]]}, "NX_WRONG_GEOMETRY", "/frame"),
        ({"frame": [[0, 0, 0], [1, 0, 0], [1, 1, 0]]}, "NX_INVALID_ARGUMENT", "/frame"),
        ({"frame": [[0, 0, 9], [1, 0, 0], [0, 1, 0]]}, "NX_WRONG_GEOMETRY", "/frame"),
        ({"layer": "bad layer"}, "NX_INVALID_ARGUMENT", "/layer"),
        ({"path": "p.txt"}, "NX_UNSUPPORTED_FILE_TYPE", "/path"),
    ],
)
def test_profile_rejections(nx, root, extra, code, field):
    sk = sketch_with(nx, [nx.line((0, 0, 5), (5, 0, 5))])
    bad = call(nx, profile.nx_export_profile, **{"source": sk.ref, "path": "p.dxf", **extra})
    assert bad.error["code"] == code and bad.error["field"] == field
    assert bad.mutation_outcome == "not_started" and not (root / "p.dxf").exists()


def test_profile_refuses_splines_curved_faces_foreign_layers_and_existing_files(nx, root):
    spline = nx.line((0, 0, 5), (1, 0, 5))
    nx.curves[spline.Tag] = ("spline", [0, 0, 5], [1, 0, 5])
    sk = sketch_with(nx, [nx.line((0, 0, 5), (5, 0, 5)), spline])
    bad = call(nx, profile.nx_export_profile, source=sk.ref, path="s.dxf")
    assert bad.error["code"] == "NX_UNSUPPORTED_GEOMETRY" and bad.mutation_outcome == "not_started"
    cyl = nx.face(kind="cylinder")
    curved = call(nx, profile.nx_export_profile, source=cyl.ref, path="s.dxf")
    assert curved.error["code"] == "NX_WRONG_GEOMETRY"
    good = sketch_with(nx, [nx.line((0, 0, 5), (5, 0, 5))])
    foreign = nx.line((0, 0, 0), (1, 0, 0))
    layer = call(nx, profile.nx_export_profile, source=good.ref, path="s.dxf",
                 layers=[[nx.ref_of(foreign, "curve"), "X"]])  # fmt: skip
    assert layer.error["field"] == "/layers/0/0"
    (root / "s.dxf").write_text("old")
    exists = call(nx, profile.nx_export_profile, source=good.ref, path="s.dxf")
    assert exists.error["code"] == "NX_ALREADY_EXISTS"
    assert (root / "s.dxf").read_text() == "old"


def test_dxf_text_writes_millimetre_units_layers_and_arcs():
    text = profile.dxf_text(
        [
            {"type": "LINE", "layer": "OUTLINE", "start": [0.0, 0.0], "end": [80.0, 0.0]},
            {"type": "ARC", "layer": "OUTLINE", "center": [80.0, 2.0], "radius": 2.0,
             "start_angle": 270.0, "end_angle": 0.0},
            {"type": "CIRCLE", "layer": "HOLES", "center": [10.0, 10.0], "radius": 2.0},
        ]
    )  # fmt: skip
    tokens = text.splitlines()
    pairs = list(zip(tokens[::2], tokens[1::2], strict=True))
    assert pairs[pairs.index(("9", "$INSUNITS")) + 1] == ("70", "4")
    kinds = [v for k, v in pairs if k == "0" and v in {"LINE", "ARC", "CIRCLE"}]
    assert kinds == ["LINE", "ARC", "CIRCLE"]
    assert ("50", "270") in pairs and ("51", "0") in pairs and ("2", "HOLES") in pairs


def test_clone_failure_in_nx_keeps_sources_and_removes_only_the_copy(nx, root):
    saved_assembly(nx)
    before = (root / "proj/parts/blk.prt").read_bytes()

    def broken(_failures):
        first = next(iter(nx.uf.Clone.names.values()))
        Path(first).write_bytes(b"partial")
        raise NXException(720103, "clone stopped")

    nx.uf.Clone.PerformClone = broken
    bad = call(nx, assembly.nx_export_assembly, part="asm", path="c", format="clone", prefix="Q_")
    assert bad.error["code"] == "NX_EXPORT_FAILED" and bad.mutation_outcome == "rolled_back"
    assert not (root / "c").exists() and (root / "proj/parts/blk.prt").read_bytes() == before
    assert nx.uf.Clone.calls[-1] == "Terminate"
    assert nx.session.Parts.LoadOptions.ComponentLoadMethod == "search"


@pytest.mark.parametrize("failure", ["unsaved", "missing", "unloaded", "outside"])
def test_clone_preflight_refuses_before_nx_clones(nx, root, failure):
    asm, blk, _plate = saved_assembly(nx)
    if failure == "unsaved":
        blk.IsModified = True
    elif failure == "missing":
        (root / "proj/parts/plate.prt").unlink()
    elif failure == "unloaded":
        asm.ComponentAssembly.RootComponent.children[0].Prototype = None
    else:
        blk.FullPath = str(root.parent / "engineer" / "blk.prt")
    bad = call(nx, assembly.nx_export_assembly, part="asm", path="c", format="clone", prefix="R_")
    codes = {"unsaved": "NX_UNSAVED_PART", "missing": "NX_FILE_NOT_FOUND",
             "unloaded": "NX_NOT_LOADED", "outside": "NX_PATH_OUTSIDE_WORKSPACE"}  # fmt: skip
    assert bad.error["code"] == codes[failure] and bad.mutation_outcome == "not_started"
    assert "Initialise" not in nx.uf.Clone.calls and not (root / "c").exists()


def test_clone_refuses_an_assembly_nx_cannot_load(nx, root):
    from types import SimpleNamespace as NS

    saved_assembly(nx)
    nx.uf.Clone.AddAssembly = lambda path: (NS(Failed=True, FileNames=["blk.prt"], Statuses=[7]),)
    bad = call(nx, assembly.nx_export_assembly, part="asm", path="c", format="clone", prefix="S_")
    assert bad.error["code"] == "NX_NOT_LOADED" and bad.error["details"]["files"] == ["blk.prt"]
    assert (
        not (root / "c").exists() and nx.session.Parts.LoadOptions.ComponentLoadMethod == "search"
    )


def test_assembly_format_follows_the_path_and_prefix(nx, root):
    asm, blk, plate = saved_assembly(nx)
    nx.structure[str((root / "proj/asm.prt").resolve()).casefold()] = [
        str(root / "proj/parts/blk.prt"), str(root / "proj/parts/plate.prt")
    ]  # fmt: skip
    zipped = call(nx, assembly.nx_export_assembly, part="asm", path="pkg/asm.zip")
    assert zipped.ok and zipped.data["format"] == "zip", zipped
    cloned = call(nx, assembly.nx_export_assembly, part="asm", path="copy", prefix="C_")
    assert cloned.ok and cloned.data["format"] == "clone", cloned
    vague = call(nx, assembly.nx_export_assembly, part="asm", path="copy3")
    assert vague.error["field"] == "/format" and vague.mutation_outcome == "not_started"
    assert ".zip" in vague.error["message"] and "prefix" in vague.error["message"]
    blk.IsModified = plate.IsModified = True
    unsaved = call(nx, assembly.nx_export_assembly, part="asm", path="p.zip")
    assert unsaved.error["code"] == "NX_UNSAVED_PART"
    assert unsaved.error["details"]["part"] == "proj/parts/blk.prt"
    assert [r["kind"] for r in unsaved.error["details"]["parts"]] == ["part", "part"]
    assert "plate.prt" in unsaved.error["message"] and "nx_save_part" in unsaved.error["hint"]


def test_profile_dxf_is_r12_that_the_import_check_accepts(nx, root):
    from nx_mcp.domains.parts_files import importing

    sk = sketch_with(nx, [
        nx.line((0, 0, 5), (50, 0, 5)), nx.line((50, 0, 5), (50, 30, 5)),
        nx.arc((25, 15, 5), 4.0),
    ])  # fmt: skip
    ok = call(nx, profile.nx_export_profile, source=sk.ref, path="cut/p.dxf", layer="CUT")
    assert ok.ok, ok
    data = (root / "cut" / "p.dxf").read_bytes()
    assert b"\r\n" in data and b"AcDb" not in data
    facts = importing.dxf_facts(data)  # NX refuses R13+ DXF without handles/OBJECTS
    assert facts["version"] == "AC1009" and facts["units"] == "mm" and facts["curves"] == 3
    assert facts["min"] == pytest.approx([0, 0, 0]) and facts["max"] == pytest.approx([50, 30, 0])


def test_profile_layer_errors_list_the_refs_of_the_source(nx, root):
    sk = sketch_with(nx, [nx.line((0, 0, 5), (5, 0, 5)), nx.line((5, 0, 5), (5, 5, 5))])
    foreign = nx.line((0, 0, 0), (1, 0, 0))
    bad = call(nx, profile.nx_export_profile, source=sk.ref, path="s.dxf",
               layers=[[nx.ref_of(foreign, "curve"), "X"]])  # fmt: skip
    assert bad.error["field"] == "/layers/0/0" and "curves (c:N)" in bad.error["message"]
    valid = bad.error["details"]["valid"]
    assert valid == [nx.ref_of(c, "curve") for c in sk.geometry]
    assert bad.error["details"]["valid_count"] == 2 and not (root / "s.dxf").exists()

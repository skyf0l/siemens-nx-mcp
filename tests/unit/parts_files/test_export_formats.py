"""nx_export_model formats beyond STEP AP214: STL, IGES, Parasolid, JT and STEP AP242."""

import pytest

from nx_mcp.domains.parts_files import exchange, neutral
from tests.kit import NXException
from tests.kit import parts as fake_parts
from tests.kit.objects import Body
from tests.unit.parts_files.support import call

BOXES = ([0, 0, 0, 40, 20, 10], [200, 0, 0, 210, 20, 30])


def modified_part(nx, rel="m.prt", units="mm", boxes=BOXES):
    part = nx.new_part(rel, units)
    part.Save()
    for box in boxes:
        body = Body("B", box=list(box))
        body.OwningPart = part
        part.Bodies.append(body)
    part.IsModified = True  # unsaved changes in memory
    return part


def export(nx, **args):
    return call(nx, exchange.nx_export_model, **args)


def assert_not_saved(ok, root, saved, part):
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert ok.data["part_saved"] is False and ok.data["part_modified"] is True
    assert part.IsModified and (root / "m.prt").read_bytes() == saved
    assert not (root / ".nx-mcp" / "staging").exists()


def test_stl_binary_from_memory(nx, root):
    part = modified_part(nx)
    saved = (root / "m.prt").read_bytes()
    ok = export(nx, part="m", path="out/m.stl", format="stl")
    assert_not_saved(ok, root, saved, part)
    stl = nx.exporters[-1]
    assert stl.OutputType == nx.nx.STLCreator.OutputTypeEnum.Binary and stl.destroyed
    assert stl.selected == list(part.Bodies)
    data = ok.data
    assert data["encoding"] == "binary" and data["triangles"] == 24 and data["bodies"] == 2
    assert data["bounds"] == {"min": [0, 0, 0], "max": [210, 20, 30]}
    assert data["tolerances"] == {"chordal": 0.058, "angular": 18.0}
    assert data["units"] == "mm" and data["file_units"] is None and data["schema"] is None
    facts = neutral.stl_facts((root / "out" / "m.stl").read_bytes())
    assert facts["triangles"] == 24


def test_stl_ascii_tolerances_and_selection(nx, root):
    part = modified_part(nx)
    ref = nx.ref_of(part.Bodies[1], "body")
    ok = export(
        nx,
        path="a.stl",
        format="stl",
        bodies=[ref],
        options={"binary": False, "chordal_tolerance": "0.01 mm", "angular_tolerance": 10},
    )
    assert ok.ok, ok
    stl = nx.exporters[-1]
    assert stl.OutputType == nx.nx.STLCreator.OutputTypeEnum.Text
    assert stl.ChordalTol == pytest.approx(0.01) and stl.AngularTol == 10
    assert stl.selected == [part.Bodies[1]]
    assert ok.data["encoding"] == "text" and ok.data["triangles"] == 12
    assert ok.data["bounds"] == {"min": [200, 0, 0], "max": [210, 20, 30]}
    # NX writes ASCII STL as <name>.stl.txt; the result is published under the asked name.
    assert (root / "a.stl").read_bytes().startswith(b"solid")
    assert not (root / "a.stl.txt").exists()


def test_stl_in_an_inch_part_reports_inches(nx, root):
    modified_part(nx, units="in", boxes=([0, 0, 0, 2, 1, 0.5],))
    ok = export(
        nx, part="m", path="i.stl", format="stl", options={"chordal_tolerance": "0.0254 mm"}
    )
    assert ok.ok, ok
    assert ok.data["units"] == "in" and ok.data["bounds"]["max"] == [2, 1, 0.5]
    assert nx.exporters[-1].ChordalTol == pytest.approx(0.001)


def test_iges_from_display_part_without_saving(nx, root, ugii):
    part = modified_part(nx)
    saved = (root / "m.prt").read_bytes()
    ok = export(nx, part="m", path="m.igs", format="iges")
    assert_not_saved(ok, root, saved, part)
    iges = nx.exporters[-1]
    assert iges.ExportFrom == nx.nx.IgesCreator.ExportFromOption.DisplayPart
    assert iges.FileSaveFlag is False and iges.InputFile == part.FullPath
    assert iges.SettingsFile == str(ugii / "IGES" / "igesexport.def")
    assert ok.data["surfaces"] == 4 and ok.data["file_units"] == "mm"
    assert ok.data["schema"] == "5.3" and ok.data["bounds"]["max"] == [210, 20, 30]


@pytest.mark.parametrize(("suffix", "encoding"), [(".x_t", "text"), (".x_b", "binary")])
def test_parasolid_text_and_binary(nx, root, suffix, encoding):
    part = modified_part(nx)
    saved = (root / "m.prt").read_bytes()
    ref = nx.ref_of(part.Bodies[0], "body")
    ok = export(nx, path="p" + suffix, format="parasolid", bodies=[ref])
    assert_not_saved(ok, root, saved, part)
    ps = nx.exporters[-1]
    assert ps.ExportFrom == nx.nx.ParasolidExporter.ExportFromOption.DisplayedPart
    assert ps.selected == [part.Bodies[0]]
    assert ps.ExportSelectionBlock.SelectionScope == nx.nx.ObjectSelector.Scope.SelectedObjects
    assert ok.data["encoding"] == encoding and ok.data["schema"] == "SCH_3601233_36001"
    assert ok.data["bounds"] is None and ok.data["bodies"] == 1


def test_jt_monolithic_from_a_saved_part(nx, root):
    part = modified_part(nx)
    unsaved = export(nx, part="m", path="m.jt", format="jt")
    assert unsaved.error["code"] == "NX_UNSAVED_PART" and unsaved.mutation_outcome == "not_started"
    assert not nx.exporters and part.IsModified
    part.IsModified = False  # saved: NX 2406 JT writes edited bodies as last saved
    ok = export(nx, part="m", path="m.jt", format="jt")
    assert ok.ok and ok.data["part_modified"] is False, ok
    jt = nx.exporters[-1]
    assert jt.JtfileStructure == nx.nx.JtCreatorFileStructure.Monolithic and jt.destroyed
    data = ok.data
    assert data["shapes"] == 2 and data["triangles"] == 24 and data["schema"] == "10.9"
    assert data["file_units"] == "mm" and data["bounds"]["min"] == [0, 0, 0]
    one = export(nx, path="one.jt", format="jt", bodies=[nx.ref_of(part.Bodies[1], "body")])
    assert one.data["shapes"] == 1 and one.data["bounds"]["min"] == [200, 0, 0]
    assert nx.exporters[-1].selected == [part.Bodies[1]]


def test_step_ap242_needs_its_license(nx, root, ugii):
    part = modified_part(nx)
    bad = export(nx, part="m", path="a.step", format="step_ap242")
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and bad.error["nx_code"] == 2
    assert nx.step_creators[-1].ExportAs == nx.nx.StepCreator.ExportAsOption.Ap242
    assert not (root / "a.step").exists() and part.IsModified
    nx.ap242_licensed = True
    ok = export(nx, part="m", path="b.step", format="step_ap242")
    assert ok.ok and ok.data["schema"] == "AP242" and ok.data["solids"] == 2, ok
    # AP214 sets its own schema even when the session's last export was AP242.
    nx.step_export_as = nx.nx.StepCreator.ExportAsOption.Ap242
    plain = export(nx, part="m", path="c.step", format="step")
    assert plain.data["schema"] == "AP214" and plain.data["file_units"] == "mm"
    assert nx.step_creators[-1].ExportAs == nx.nx.StepCreator.ExportAsOption.Ap214


def test_extent_mismatch_fails_and_publishes_nothing(nx, root):
    modified_part(nx)
    nx.uf.ModlGeneral.AskBoundingBoxExact = lambda tag, _c: (
        [0, 0, 0],
        [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        [500, 20, 30],
    )
    bad = export(nx, part="m", path="x.stl", format="stl")
    assert (
        bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.error["details"]["check"] == "extent"
    )
    assert bad.mutation_outcome == "rolled_back" and not (root / "x.stl").exists()


def test_rotated_wcs_checks_containment_only(nx, root):
    part = modified_part(nx)
    part.WCS.CoordinateSystem.Orientation.Element = fake_parts.NS(
        Xx=0, Xy=1, Xz=0, Yx=-1, Yy=0, Yz=0, Zx=0, Zy=0, Zz=1
    )
    ok = export(nx, part="m", path="r.stl", format="stl")
    assert ok.ok, ok


def test_wrong_file_units_and_unreadable_files_fail(nx, root, monkeypatch):
    modified_part(nx).IsModified = False
    real = fake_parts.JtCreator._file
    monkeypatch.setattr(
        fake_parts.JtCreator,
        "_file",
        lambda self, out, boxes, units: real(self, out, boxes, "in"),
    )
    bad = export(nx, part="m", path="u.jt", format="jt")
    assert bad.error["details"]["check"] == "file_units" and not (root / "u.jt").exists()
    monkeypatch.setattr(fake_parts.JtCreator, "_file", lambda self, out, b, u: (out, b"junk"))
    junk = export(nx, part="m", path="v.jt", format="jt")
    assert junk.error["code"] == "NX_VERIFICATION_FAILED"
    assert junk.error["details"]["check"] == "file_content"


def test_no_file_no_bodies_and_native_errors(nx, root, ugii):
    nx.new_part("e.prt")
    empty = export(nx, part="e", path="e.stl", format="stl")
    assert empty.error["code"] == "NX_INVALID_ARGUMENT" and empty.error["field"] == "/bodies"
    assert empty.mutation_outcome == "not_started"
    modified_part(nx)
    nx.export_writes = False
    none = export(nx, part="m", path="n.igs", format="iges")
    assert none.error["code"] == "NX_EXPORT_FAILED" and none.mutation_outcome == "rolled_back"
    assert none.error["details"]["translator_log"] == "translator log\n"
    nx.export_writes = True
    nx.export_error = NXException(3, "Translator crashed")
    crash = export(nx, part="m", path="n.x_t", format="parasolid")
    assert crash.error["code"] == "NX_EXPORT_FAILED" and crash.error["nx_code"] == 3


def test_a_translator_that_saves_is_caught(nx, root, ugii, monkeypatch):
    modified_part(nx)
    real = fake_parts.IgesCreator.Commit

    def saving(self):
        self.FileSaveFlag = True
        return real(self)

    monkeypatch.setattr(fake_parts.IgesCreator, "Commit", saving)
    bad = export(nx, part="m", path="s.igs", format="iges")
    assert bad.error["code"] == "NX_VERIFICATION_FAILED"
    assert bad.error["details"]["check"] == "part_modified"


def test_a_builder_is_destroyed_when_configuring_fails(nx, root, monkeypatch):
    modified_part(nx)

    def refuse(self):
        self.ExportSelectionBlock = fake_parts.NS(
            Add=lambda objs: (_ for _ in ()).throw(NXException(5, "Selection refused"))
        )

    monkeypatch.setattr(fake_parts.StlCreator, "_refuse", refuse, raising=False)
    real = fake_parts.StlCreator.__init__

    def init(self, fake):
        real(self, fake)
        self._refuse()

    monkeypatch.setattr(fake_parts.StlCreator, "__init__", init)
    bad = export(nx, part="m", path="f.stl", format="stl")
    assert bad.error["code"] == "NX_EXPORT_FAILED" and bad.error["nx_code"] == 5
    assert nx.exporters[-1].destroyed


def test_step_ap242_without_a_file_hints_at_the_license(nx, root, ugii):
    modified_part(nx).IsModified = False  # NX runs the external step242 job
    nx.ap242_licensed, nx.export_writes = True, False
    bad = export(nx, part="m", path="a.step", format="step_ap242")
    assert bad.error["code"] == "NX_EXPORT_FAILED" and "license" in bad.error["hint"]


def test_an_ap242_interrupt_without_a_license_message_is_an_export_failure(nx, root, ugii):
    from nx_mcp.core import evidence

    modified_part(nx)
    nx.ap242_licensed = True
    nx.export_error = NXException(2, "Interrupt")  # e.g. the engineer pressed Esc
    bad = export(nx, part="m", path="a.step", format="step_ap242")
    assert bad.error["code"] == "NX_EXPORT_FAILED" and bad.error["nx_code"] == 2, bad
    assert evidence.licenses() == []
    nx.ap242_licensed, nx.export_error = False, None  # the syslog says the license is missing
    refused = export(nx, part="m", path="a.step", format="step_ap242")
    assert refused.error["code"] == "NX_LICENSE_UNAVAILABLE", refused
    assert "STEP AP242 License not found" in refused.error["message"]
    evidence.forget_licenses()


def test_a_failure_after_the_export_is_published_is_partial(nx, root, monkeypatch):
    modified_part(nx)

    def broken(*a, **k):
        raise OSError("disk went away")

    monkeypatch.setattr(exchange.files, "metadata", broken)
    bad = export(nx, part="m", path="out/m.stl", format="stl")
    assert bad.mutation_outcome == "partial", bad  # not rolled_back: the file stays
    assert (root / "out" / "m.stl").is_file()
    assert not (root / ".nx-mcp" / "staging").exists()


def test_an_ascii_stl_written_with_an_extra_suffix_is_published(nx, root):
    modified_part(nx)
    ok = export(nx, part="m", path="t.stl", format="stl", options={"binary": False})
    assert ok.ok, ok
    assert (root / "t.stl").is_file() and not list(root.glob("t.stl.*"))

from pathlib import Path

import pytest

from nx_mcp.domains.parts_files import exchange
from tests.kit import NXException
from tests.kit.objects import Body
from tests.unit.parts_files.support import call

STEP = "ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n#1=PRODUCT('blk','blk','',(#2));\nENDSEC;\nEND-ISO-10303-21;\n"


def modified_part(nx, rel="m.prt", bodies=2):
    part = nx.new_part(rel)
    part.Save()
    for _ in range(bodies):
        body = Body("B")
        body.OwningPart = part
        part.Bodies.append(body)
    part.IsModified = True  # unsaved changes in memory
    return part


def test_export_writes_memory_state_and_never_saves(nx, root, ugii):
    part = modified_part(nx)
    saved = (root / "m.prt").read_bytes()
    ok = call(nx, exchange.nx_export_model, path="out/m.step", format="step")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    creator = nx.step_creators[-1]
    assert creator.ExportFrom == nx.nx.StepCreator.ExportFromOption.DisplayPart
    assert creator.FileSaveFlag is False and creator.destroyed
    assert creator.InputFile == part.FullPath  # NX 2406 reads an unmodified part from it
    assert creator.SettingsFile == str(ugii / "STEP214UG" / "ugstep214.def")
    assert ok.data["solids"] == 2 and ok.data["part_saved"] is False
    assert ok.data["part_modified"] is True and part.IsModified
    assert (root / "m.prt").read_bytes() == saved
    text = (root / "out" / "m.step").read_text()
    assert text.count("MANIFOLD_SOLID_BREP") == 2 and ok.data["path"] == "out/m.step"
    assert not (root / ".nx-mcp" / "staging").exists()  # staging cleaned


def test_export_selected_bodies(nx, ugii):
    part = modified_part(nx)
    ref = nx.ref_of(part.Bodies[1], "body")
    ok = call(nx, exchange.nx_export_model, path="one.stp", format="step", bodies=[ref])
    assert ok.data["solids"] == 1 and ok.data["bodies"] == 1
    assert nx.step_creators[-1].selected == [part.Bodies[1]]
    twice = call(nx, exchange.nx_export_model, path="two.stp", format="step", bodies=[ref, ref])
    assert twice.error["code"] == "NX_INVALID_ARGUMENT" and twice.mutation_outcome == "not_started"


def test_export_rejections(nx, root, ugii, monkeypatch):
    part = modified_part(nx)
    other = nx.new_part("o.prt")
    nx.session.Parts.Display = other
    nx.session.Parts.Work = part
    wrong = call(nx, exchange.nx_export_model, path="a.step", format="step")
    assert (
        wrong.error["code"] == "NX_WRONG_PART_CONTEXT" and wrong.mutation_outcome == "not_started"
    )
    nx.session.Parts.Display = part
    (root / "e.step").write_text("x")
    exists = call(nx, exchange.nx_export_model, path="e.step", format="step")
    assert exists.error["code"] == "NX_ALREADY_EXISTS"
    assert call(nx, exchange.nx_export_model, path="a.igs", format="step").error["code"] == (
        "NX_UNSUPPORTED_FILE_TYPE"
    )
    nx.export_writes = False
    empty = call(nx, exchange.nx_export_model, path="a.step", format="step")
    assert empty.error["code"] == "NX_EXPORT_FAILED" and empty.mutation_outcome == "rolled_back"
    assert empty.error["details"]["translator_log"] == "translator log\n"
    assert not (root / "a.step").exists()
    nx.export_writes = True
    nx.export_error = NXException(1, "Translator crashed")
    crashed = call(nx, exchange.nx_export_model, path="a.step", format="step")
    assert crashed.error["code"] == "NX_EXPORT_FAILED" and crashed.error["nx_code"] == 1
    monkeypatch.delenv("UGII_BASE_DIR")
    nx.export_error = None
    unset = call(nx, exchange.nx_export_model, path="a.step", format="step")
    assert unset.error["code"] == "NX_EXPORT_FAILED" and unset.mutation_outcome == "not_started"


def test_export_detects_a_part_saved_by_the_translator(nx, ugii, monkeypatch):
    from tests.kit import parts as fake_parts

    real = fake_parts.StepCreator.Commit

    def saving_commit(self):
        self.FileSaveFlag = True  # a translator that saves the part anyway
        return real(self)

    monkeypatch.setattr(fake_parts.StepCreator, "Commit", saving_commit)
    modified_part(nx)
    bad = call(nx, exchange.nx_export_model, path="a.step", format="step")
    assert bad.error["code"] == "NX_VERIFICATION_FAILED"
    assert bad.error["details"]["check"] == "part_modified"


def test_import_into_the_work_part(nx, root, ugii):
    part = nx.new_part("w.prt")
    (root / "in.step").write_text(STEP)
    ok = call(
        nx, exchange.nx_import_model, path="in.step", format="step", operation_id="op_import_1"
    )
    assert ok.ok and ok.mutation_outcome == "committed", ok
    importer = nx.step_importers[-1]
    assert importer.OutputFile == str(root / "imports" / "op_import_1" / "output.prt")
    assert importer.FlattenAssembly is True and importer.FileOpenFlag is False
    assert importer.ImportTo == nx.nx.Step214Importer.ImportToOption.WorkPart
    assert importer.destroyed and ok.data["body_count"] == 1 and len(part.Bodies) == 1
    assert ok.data["staging"] == "imports/op_import_1"
    assert ok.changes["references"] == "preserved"


def test_import_into_a_new_part(nx, root, ugii):
    (root / "in.step").write_text(STEP)
    ok = call(nx, exchange.nx_import_model, path="in.step", format="step", target="new_part",
              new_part_path="imp/new.prt")  # fmt: skip
    assert ok.ok, ok
    work = nx.session.Parts.Work
    assert Path(work.FullPath) == root / "imp" / "new.prt" and len(work.Bodies) == 1
    assert ok.data["target"] == "new_part"


def test_import_rejections_and_rollback(nx, root, ugii):
    nx.new_part("w.prt")
    (root / "in.step").write_text(STEP)
    (root / "cut.step").write_text(STEP[:-20])
    truncated = call(nx, exchange.nx_import_model, path="cut.step", format="step")
    assert truncated.error["code"] == "NX_INVALID_ARGUMENT" and truncated.error["field"] == "/path"
    assert truncated.mutation_outcome == "not_started"
    both = call(nx, exchange.nx_import_model, path="in.step", format="step", target="new_part")
    assert both.error["field"] == "/new_part_path"
    nx.import_bodies = 0
    loaded = len(nx.session.Parts)
    empty = call(nx, exchange.nx_import_model, path="in.step", format="step",
                 target="new_part", new_part_path="imp/n2.prt", operation_id="op_import_2")  # fmt: skip
    assert empty.error["code"] == "NX_INCOMPLETE_RESULT" and empty.mutation_outcome == "rolled_back"
    assert len(nx.session.Parts) == loaded and not (root / "imports" / "op_import_2").exists()
    assert empty.error["details"]["translator"] == "Step214Importer"


def test_import_refuses_loaded_product_names_without_flatten(nx, root, ugii):
    nx.new_part("blk.prt")
    asm_step = STEP.replace(
        "DATA;\n", "DATA;\n#9=NEXT_ASSEMBLY_USAGE_OCCURRENCE('1','','',#1,#1,$);\n"
    )
    (root / "asm.step").write_text(asm_step)
    bad = call(nx, exchange.nx_import_model, path="asm.step", format="step", flatten=False)
    assert bad.error["code"] == "NX_NAME_CONFLICT" and bad.mutation_outcome == "not_started"
    assert bad.error["details"]["loaded_parts"] == ["blk.prt"]
    assert not nx.step_importers


@pytest.mark.parametrize(
    ("fmt", "path"), [("iges", "a.step"), ("stl", "a.igs"), ("parasolid", "a.jt"), ("jt", "a.x_t")]
)
def test_export_suffix_must_match_the_format(nx, root, fmt, path):
    nx.new_part("w.prt")
    bad = call(nx, exchange.nx_export_model, path=path, format=fmt)
    assert bad.error["code"] == "NX_UNSUPPORTED_FILE_TYPE" and bad.error["field"] == "/path"
    assert bad.mutation_outcome == "not_started" and not nx.exporters


def test_unknown_format_and_foreign_options_are_refused(nx):
    nx.new_part("w.prt")
    bad = call(nx, exchange.nx_export_model, path="a.dxf", format="dxf")
    assert bad.error["category"] == "invalid_argument"
    opts = call(
        nx, exchange.nx_export_model, path="a.step", format="step", options={"binary": True}
    )
    assert opts.error["code"] == "NX_INVALID_ARGUMENT" and opts.error["field"] == "/options/binary"

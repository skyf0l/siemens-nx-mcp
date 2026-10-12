from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import parts
from nx_mcp.workspace import Workspace
from tests.kit import fake_nx
from tests.kit.parts import with_parts


@pytest.fixture
def nx(tmp_path):
    fake = with_parts(fake_nx(), tmp_path)
    fake.tx.workspace = Workspace(tmp_path)
    fake.tx.checkpoint = lambda *a, **k: None
    return fake


def test_check_new_name_refuses_loaded_basenames(nx, tmp_path):
    a = nx.new_part("p/a.prt")
    with pytest.raises(NXError) as same:
        parts.check_new_name(nx.tx, tmp_path / "p" / "a.prt")
    assert same.value.code == "NX_ALREADY_EXISTS"
    with pytest.raises(NXError) as other:
        parts.check_new_name(nx.tx, tmp_path / "q" / "A.prt", field="/x")
    assert other.value.code == "NX_NAME_CONFLICT" and other.value.field == "/x"
    assert other.value.details["loaded_part_path"] == a.FullPath
    parts.check_new_name(nx.tx, tmp_path / "q" / "a.prt", ignore=a)  # its own new name
    parts.check_new_name(nx.tx, tmp_path / "q" / "b.prt")


def test_loaded_finds_a_part_by_file(nx, tmp_path):
    a = nx.new_part("a.prt")
    assert parts.loaded(nx.tx, tmp_path / "A.PRT") is a
    assert parts.loaded(nx.tx, tmp_path / "b.prt") is None


def test_a_part_nx_no_longer_has_is_a_stale_reference(nx, tmp_path):
    # NX 2406: a closed part's wrapper reports tag 0 and FullPath raises "Object no
    # longer exists"; file_of (and so close and in_workspace) report it by code.
    a = nx.new_part("a.prt")
    assert parts.file_of(a) == a.FullPath and parts.file_of(None) == ""
    a.Tag = 0
    for check in (parts.file_of, lambda p: parts.in_workspace(nx.tx, p)):
        with pytest.raises(NXError) as gone:
            check(a)
        assert gone.value.code == "NX_STALE_REFERENCE"
        assert gone.value.details["reason"] == "deleted_in_nx"


def test_in_workspace_refuses_other_files(nx, tmp_path):
    a = nx.new_part("a.prt")
    assert parts.in_workspace(nx.tx, a) == (tmp_path / "a.prt").resolve()
    outside = NS(FullPath=str(tmp_path.parent / "engineer.prt"))
    with pytest.raises(NXError) as caught:
        parts.in_workspace(nx.tx, outside)
    assert caught.value.code == "NX_PATH_OUTSIDE_WORKSPACE" and caught.value.field == "/part"
    reserved = NS(FullPath=str(tmp_path / ".NX-MCP" / "x.prt"))
    with pytest.raises(NXError):
        parts.in_workspace(nx.tx, reserved)
    unsaved = NS(FullPath="")
    assert parts.in_workspace(nx.tx, unsaved, allow_unsaved=True) is None
    with pytest.raises(NXError):
        parts.in_workspace(nx.tx, unsaved)


def test_activate_sets_display_before_work(nx):
    a, b = nx.new_part("a.prt"), nx.new_part("b.prt")
    parts.activate(nx.tx, a, display=False)
    assert nx.session.Parts.Work is a and nx.session.Parts.Display is b
    parts.activate(nx.tx, a)
    assert nx.session.Parts.Display is a


def test_components_dependents_and_prototype_files(nx):
    asm, blk = nx.new_part("asm.prt"), nx.new_part("blk.prt")
    occurrence = nx.attach(asm, blk)
    walked = parts.components(asm)
    assert [(c, names) for c, names in walked] == [(occurrence, ["BLK"])]
    assert parts.prototype_file(occurrence) == blk.FullPath
    assert parts.dependents(nx.tx, blk) == [asm.FullPath]
    assert parts.dependents(nx.tx, asm) == []


def test_load_components_restores_partial_loading_and_reports_failures(nx):
    asm, blk = nx.new_part("asm.prt"), nx.new_part("blk.prt")
    nx.attach(asm, blk)
    options = nx.session.Parts.LoadOptions
    assert parts.load_components(nx.tx, asm) == 1
    assert options.UsePartialLoading is True
    failing = NS(
        NumberUnloadedParts=1, GetPartName=lambda i: "x.prt", GetStatus=lambda i: 7,
        Dispose=lambda: None,
    )  # fmt: skip
    asm.ComponentAssembly.OpenComponents = lambda option, comps: (failing, None)
    with pytest.raises(NXError) as caught:
        parts.load_components(nx.tx, asm)
    assert caught.value.code == "NX_IMPORT_FAILED"
    assert caught.value.details["parts"] == [{"path": "x.prt", "nx_code": 7}]
    assert options.UsePartialLoading is True


def test_saving_displays_a_sheet_for_cgm_data_and_restores(nx):
    other, part = nx.new_part("other.prt"), nx.new_part("dwg.prt")
    nx.session.Parts.Work = nx.session.Parts.Display = other
    sheet = NS(Tag=99, Name="SHT1", opened=0)

    def open_sheet():
        sheet.opened += 1
        part.DrawingSheets.CurrentDrawingSheet = sheet

    sheet.Open = open_sheet
    exits = []
    part.Drafting = NS(ExitDraftingApplication=lambda: exits.append(1))
    part.DrawingSheets.append(sheet)
    with parts.saving(nx.tx, part):  # no CGM data: nothing happens
        assert sheet.opened == 0
    part.SaveOptions.DrawingCgmData = True
    part.ModelingViews = NS(WorkView=NS())

    def exit_drafting():
        exits.append(1)
        part.DrawingSheets.CurrentDrawingSheet = None

    part.Drafting = NS(ExitDraftingApplication=exit_drafting)
    with parts.saving(nx.tx, part):
        assert sheet.opened == 1 and nx.session.Parts.Display is part
    assert exits == [1] and part.DrawingSheets.CurrentDrawingSheet is None
    assert nx.session.Parts.Work is other and nx.session.Parts.Display is other


def test_saving_refuses_an_active_sketch(nx):
    part = nx.new_part("dwg.prt")
    part.DrawingSheets.append(NS(Tag=5, Name="S", Open=lambda: None))
    part.SaveOptions.DrawingCgmData = True
    nx.session.ActiveSketch = object()
    with pytest.raises(NXError) as caught, parts.saving(nx.tx, part):
        pass
    assert caught.value.code == "NX_SKETCH_ACTIVE"


def test_work_without_display_only_inside_the_display_assembly(nx):
    asm, blk, other = nx.new_part("asm.prt"), nx.new_part("blk.prt"), nx.new_part("other.prt")
    nx.session.Parts.Display = nx.session.Parts.Work = asm
    nx.attach(asm, blk)
    assert parts.can_be_work(nx.tx, asm) and parts.can_be_work(nx.tx, blk)
    assert not parts.can_be_work(nx.tx, other)
    shown = parts.present(nx.tx, blk, display=False)  # a component: NX takes it as work
    assert shown.work and not shown.active and nx.session.Parts.Work is blk
    nx.session.Parts.SetWork(asm)
    shown = parts.present(nx.tx, other, display=False)  # elsewhere: never refused (bench3t)
    assert not shown.work and shown.kept is asm and nx.session.Parts.Work is asm
    nx.session.Parts.Display = None
    assert parts.can_be_work(nx.tx, other)  # nothing displayed: any part may become work part

from pathlib import Path
from unittest.mock import Mock

import pytest

from nx_mcp.domains.parts_files import parts as tools
from tests.kit import NXException
from tests.kit.kernel import services
from tests.unit.parts_files.support import call


def test_create_part_loads_activates_and_reports_units(nx, root):
    nx.session.Parts.remove(nx.part)  # solo: the agent's only part
    ok = call(nx, tools.nx_create_part, path="proj/base.prt", units="in")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    part = nx.session.Parts.Work
    assert Path(part.FullPath) == root / "proj" / "base.prt"
    assert nx.session.Parts.Display is part  # made without display, then shown
    assert nx.file_news[-1].MakeDisplayedPart is False and nx.session.shown[-1][0] == "retain"
    assert part.template == "model-plain-1-inch-template.prt"
    assert ok.data["units"] == "in" and ok.data["path"] == "proj/base.prt"
    assert ok.data["work"] and ok.data["display"] and ok.data["part"]["kind"] == "part"


def test_create_part_without_display_only_loads_it(nx, root):
    work = nx.session.Parts.Work
    mark = nx.session.SetUndoMark(1, "before")
    ok = call(nx, tools.nx_create_part, path="agents/b/b.prt", display=False)
    assert ok.ok and not ok.data["work"] and not ok.data["display"], ok
    assert nx.session.Parts.Work is work and nx.session.Parts.Display is work
    assert ok.data["path"] == "agents/b/b.prt" and mark in nx.session.marks
    assert nx.session.shown == []
    say = {"path": "b.prt"}.get
    assert tools.nx_create_part.spec.summary({"path": "b.prt", "display": False}, say) == (
        "Create part b.prt in mm, not displayed"
    )


def test_create_part_refuses_files_and_loaded_basenames_before_nx(nx, root):
    nx.new_part("proj/base.prt")
    loaded = len(nx.session.Parts)
    same = call(nx, tools.nx_create_part, path="proj/base.prt")
    assert same.error["code"] == "NX_ALREADY_EXISTS" and same.mutation_outcome == "not_started"
    other = call(nx, tools.nx_create_part, path="other/BASE.prt")
    assert other.error["code"] == "NX_NAME_CONFLICT" and other.mutation_outcome == "not_started"
    assert other.error["details"]["loaded_part_path"].endswith("base.prt")
    assert not (root / "other").exists() and len(nx.session.Parts) == loaded
    (root / "x.prt").write_bytes(b"part")
    exists = call(nx, tools.nx_create_part, path="x.prt")
    assert exists.error["code"] == "NX_ALREADY_EXISTS" and exists.error["field"] == "/path"
    for path, code in [("../up.prt", "NX_PATH_OUTSIDE_WORKSPACE"),
                       (".nx-mcp/a.prt", "NX_PATH_RESERVED"),
                       ("a.step", "NX_UNSUPPORTED_FILE_TYPE")]:  # fmt: skip
        assert call(nx, tools.nx_create_part, path=path).error["code"] == code


def test_create_part_with_wrong_units_is_closed_again(nx, monkeypatch):
    from tests.kit import parts as fake_parts

    real = fake_parts.FileNew.Commit

    def wrong_units(self):
        self.Units = "in"
        return real(self)

    monkeypatch.setattr(fake_parts.FileNew, "Commit", wrong_units)
    before = len(nx.session.Parts)
    bad = call(nx, tools.nx_create_part, path="u.prt", units="mm")
    assert (
        bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.error["details"]["check"] == "units"
    )
    assert bad.mutation_outcome == "rolled_back" and len(nx.session.Parts) == before


def test_open_part_opens_or_reuses(nx, root):
    a = nx.new_part("a.prt")
    a.Save()
    a.Close()
    opened = call(nx, tools.nx_open_part, path="a.prt")
    assert opened.ok and opened.data["already_loaded"] is False
    assert nx.opened_files == [str(root / "a.prt")]
    again = call(nx, tools.nx_open_part, path="A.PRT", display=False)
    assert again.data["already_loaded"] is True and nx.opened_files == [str(root / "a.prt")]
    assert again.data["part"]["id"] == opened.data["part"]["id"]


def test_open_part_loads_components_and_refuses_a_clashing_basename(nx, root):
    blk = nx.new_part("parts/blk.prt")
    blk.Save()
    asm = nx.new_part("asm.prt")
    nx.attach(asm, blk)
    asm.Save()
    asm.Close()  # NX unloads the prototype nobody uses any more
    assert blk not in nx.session.Parts
    nx.structure[str((root / "asm.prt").resolve()).casefold()] = [str(root / "parts/blk.prt")]
    ok = call(nx, tools.nx_open_part, path="asm.prt", load_components=True)
    assert ok.ok and ok.data["components_loaded"] == 1
    assert nx.session.Parts.LoadOptions.UsePartialLoading is True
    (root / "other").mkdir()
    (root / "other" / "blk.prt").write_bytes(b"x")
    clash = call(nx, tools.nx_open_part, path="other/blk.prt")
    assert clash.error["code"] == "NX_NAME_CONFLICT" and clash.mutation_outcome == "not_started"
    missing = call(nx, tools.nx_open_part, path="none.prt")
    assert missing.error["code"] == "NX_FILE_NOT_FOUND"


def test_activate_part_sets_work_and_display(nx):
    a, b = nx.new_part("a.prt"), nx.new_part("b.prt")
    ref = nx.ref_of(a, "part")
    outside = call(nx, tools.nx_activate_part, part=ref, display=False)  # never refused
    assert outside.ok and not outside.data["work"] and nx.session.Parts.Work is b, outside
    assert any('"part": "a.prt"' in w["message"] for w in outside.warnings), outside.warnings
    nx.attach(b, a)  # a component of the display part may be the work part alone
    work_only = call(nx, tools.nx_activate_part, part=ref, display=False)
    assert work_only.ok and work_only.data["work"] and not work_only.data["display"]
    assert nx.session.Parts.Display is b
    tab = call(nx, tools.nx_activate_part, part=ref)  # two agent parts: b's tab stays active
    assert tab.ok and not tab.data["display"] and tab.data["work"], tab  # a: work as before
    assert not tab.warnings, tab  # a is the work part: nothing to say
    assert nx.session.Parts.Display is b and nx.session.displayed() == [b, a]
    both = call(nx, tools.nx_activate_part, part=ref, display=True)
    assert both.data["display"] and nx.session.Parts.Display is a
    assert nx.session.displayed() == [a, b]  # a's own tab, b's tab kept
    none = call(nx, tools.nx_activate_part, part=ref, work=False, display=False)
    assert none.error["field"] == "/work" and none.mutation_outcome == "not_started"
    stale = call(nx, tools.nx_activate_part, part="p:999999")
    assert stale.error["category"] == "stale_reference" and stale.mutation_outcome == "not_started"
    kind = call(nx, tools.nx_activate_part, part=nx.ref_of(nx.block(1, 1, 1), "body"))
    assert kind.error["code"] == "NX_WRONG_KIND"


def test_list_parts_filters_and_pages(nx, root):
    a = nx.new_part("p/a.prt")
    nx.new_part("q/b.prt")
    a.IsModified = True
    every = call(nx, tools.nx_list_parts)
    assert every.mutation_outcome == "not_applicable" and every.page["total"] == 3
    rows = {r["path"]: r for r in every.data["items"]}
    assert rows["q/b.prt"]["work"] and rows["q/b.prt"]["display"] and rows["q/b.prt"]["owned"]
    assert rows["p/a.prt"]["modified"] and rows["p/a.prt"]["units"] == "mm"
    assert call(nx, tools.nx_list_parts, path_prefix="P/").page["total"] == 1
    absolute = str(root / "q").replace("/", "\\")
    assert call(nx, tools.nx_list_parts, path_prefix=absolute).page["total"] == 1
    assert call(nx, tools.nx_list_parts, modified=True).page["total"] == 1
    assert call(nx, tools.nx_list_parts, active_only=True).page["total"] == 1
    paged = call(nx, tools.nx_list_parts, limit=1, offset=1)
    assert paged.page == {"offset": 1, "limit": 1, "returned": 1, "total": 3, "next_offset": 2}


def test_save_in_place_is_audited(nx, root):
    part = nx.new_part("s.prt")
    part.IsModified = True
    ok = call(nx, tools.nx_save_part, part=nx.ref_of(part, "part"))
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert ok.data["saved_files"] == ["s.prt"] and (root / "s.prt").is_file()
    assert ok.warnings[0]["message"].startswith("Saving expires")
    assert not part.IsModified


def test_save_as_writes_a_new_file_and_never_overwrites(nx, root):
    part = nx.new_part("s.prt")
    ref = nx.ref_of(part, "part")
    ok = call(nx, tools.nx_save_part, part=ref, path="v2/s_v2.prt")
    assert ok.ok and ok.data["saved_as"] and ok.data["path"] == "v2/s_v2.prt"
    assert Path(part.FullPath) == root / "v2" / "s_v2.prt" and (root / "v2" / "s_v2.prt").is_file()
    (root / "taken.prt").write_bytes(b"someone else's file")
    taken = call(nx, tools.nx_save_part, part=ref, path="taken.prt")
    assert taken.error["code"] == "NX_ALREADY_EXISTS" and taken.mutation_outcome == "not_started"
    assert "omit path" in taken.error["hint"] and taken.error["field"] == "/path"
    assert (root / "taken.prt").read_bytes() == b"someone else's file"
    nx.new_part("other.prt")
    clash = call(nx, tools.nx_save_part, part=ref, path="z/OTHER.prt")
    assert clash.error["code"] == "NX_NAME_CONFLICT" and clash.mutation_outcome == "not_started"


def test_save_refuses_parts_outside_the_workspace_and_not_owned(nx, monkeypatch):
    from nx_mcp.core import kernel

    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)
    outside = nx.session.Parts.Work  # the fake's default part lives outside the workspace
    bad = call(nx, tools.nx_save_part)
    assert (
        bad.error["code"] == "NX_PATH_OUTSIDE_WORKSPACE" and bad.mutation_outcome == "not_started"
    )
    assert outside.FullPath
    mine = nx.new_part("mine.prt")
    svc = services(nx)
    svc.dev = False
    svc.owns = lambda part: False
    refused = call(nx, tools.nx_save_part, part=nx.ref_of(mine, "part"))
    assert refused.error["code"] == "NX_ENGINEER_PART" and refused.error["field"] == "/part"
    assert refused.mutation_outcome == "not_started" and "Allow agent" in refused.error["hint"]
    closing = call(nx, tools.nx_close_part, part=nx.ref_of(mine, "part"), save=False)
    assert closing.error["code"] == "NX_ENGINEER_PART" and mine in nx.session.Parts
    svc.granted = lambda part: True  # the engineer's grant lets the agent save, never close
    assert call(nx, tools.nx_save_part, part=nx.ref_of(mine, "part")).error["code"] == (
        "NX_PART_NOT_GRANTED"  # gate 6 next: this fake's owns() still says no
    )
    closing = call(nx, tools.nx_close_part, part=nx.ref_of(mine, "part"), save=False)
    assert closing.error["code"] == "NX_ENGINEER_PART"


def test_failed_native_save_is_partial(nx):
    part = nx.new_part("f.prt")
    nx.save_error = NXException(3515001, "Disk full")
    bad = call(nx, tools.nx_save_part, part=nx.ref_of(part, "part"))
    assert bad.error["code"] == "NX_SAVE_FAILED" and bad.error["nx_code"] == 3515001
    assert bad.mutation_outcome == "partial"


def test_save_verification_catches_other_files_changing(nx, root):
    other = nx.new_part("o.prt")
    other.Save()
    part = nx.new_part("t.prt")
    real = type(part).Save

    def save_both(self, *args):
        (root / "o.prt").write_bytes(b"changed behind our back")
        return real(self, *args)

    part.Save = save_both.__get__(part)
    bad = call(nx, tools.nx_save_part, part=nx.ref_of(part, "part"))
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.error["details"]["check"] == "save"
    assert bad.error["details"]["unexpected_changes"] == ["o.prt"]
    assert bad.mutation_outcome == "partial"


def test_close_part_without_saving_forgets_its_references(nx):
    part = nx.new_part("c.prt")
    body = nx.block(1, 1, 1)
    body.OwningPart = part
    body_ref = nx.ref_of(body, "body")
    key = f"part_{part.Tag}"
    ok = call(nx, tools.nx_close_part, part=nx.ref_of(part, "part"), save=False)
    assert ok.ok and ok.data["closed_count"] == 1 and ok.data["saved"] is False
    assert part not in nx.session.Parts and body_ref not in nx.refs
    assert nx.forgotten == [key] and part.Tag == 0  # NX: a closed part reports tag 0


def test_close_part_never_saves_by_default_and_refuses_unsaved_changes(nx):
    assert tools.nx_close_part.spec.params["save"].default is None
    clean = nx.new_part("clean.prt")
    clean.Save = Mock(side_effect=AssertionError("saved"))
    ok = call(nx, tools.nx_close_part, part=nx.ref_of(clean, "part"))
    assert ok.ok and ok.data["saved"] is False and clean not in nx.session.Parts
    dirty = nx.new_part("dirty.prt")
    dirty.IsModified = True
    ref = nx.ref_of(dirty, "part")
    refused = call(nx, tools.nx_close_part, part=ref)
    assert refused.error["code"] == "NX_UNSAVED_PART" and refused.error["field"] == "/save"
    assert "save=false" in refused.error["message"] and dirty in nx.session.Parts
    assert refused.mutation_outcome == "not_started"
    discarded = call(nx, tools.nx_close_part, part=ref, save=False)
    assert discarded.ok and discarded.data["saved"] is False and dirty not in nx.session.Parts


def test_close_part_needs_an_explicit_part_never_the_work_part(nx):
    part = nx.new_part("work.prt")
    assert tools.nx_close_part.spec.params["part"].required
    refused = call(nx, tools.nx_close_part, save=False)
    assert refused.error["code"] == "NX_INVALID_ARGUMENT" and refused.error["field"] == "/part"
    assert refused.mutation_outcome == "not_started" and part in nx.session.Parts


def test_close_part_refuses_a_prototype_in_use_and_closes_the_assembly(nx):
    blk = nx.new_part("blk.prt")
    asm = nx.new_part("asm.prt")
    nx.attach(asm, blk)
    busy = call(nx, tools.nx_close_part, part=nx.ref_of(blk, "part"), save=False)
    assert busy.error["code"] == "NX_PART_IN_USE" and busy.mutation_outcome == "not_started"
    assert busy.error["details"]["parent_assemblies"] == ["asm.prt"]
    ok = call(nx, tools.nx_close_part, part=nx.ref_of(asm, "part"), save=True)
    assert ok.ok and ok.data["closed_count"] == 2  # NX unloaded the unused prototype too
    assert {r["name"] for r in ok.data["closed"]} == {"asm", "blk"}


# ---- drawing preview (CGM) data survives saves: the sheet is shown while saving ----
@pytest.fixture
def drawing_part(nx):
    from types import SimpleNamespace as NS

    from tests.kit.objects import Object

    part = nx.new_part("dwg.prt")
    sheet = Object("SHT1")
    sheet.opened = 0

    def show():
        sheet.opened += 1
        part.DrawingSheets.CurrentDrawingSheet = sheet

    def leave():
        part.DrawingSheets.CurrentDrawingSheet = None

    sheet.Open = show
    part.DrawingSheets.append(sheet)
    part.SaveOptions.DrawingCgmData = True
    part.Drafting = NS(ExitDraftingApplication=leave)
    part.ModelingViews = NS(WorkView=NS())
    part.sheet = sheet
    return part


def test_save_shows_the_sheet_for_cgm_data_and_returns_to_modeling(nx, drawing_part):
    seen = []
    real = type(drawing_part).Save

    def save(self, *args):
        seen.append(self.DrawingSheets.CurrentDrawingSheet)
        return real(self, *args)

    drawing_part.Save = save.__get__(drawing_part)
    drawing_part.IsModified = True
    ok = call(nx, tools.nx_save_part, part=nx.ref_of(drawing_part, "part"))
    assert ok.ok and seen == [drawing_part.sheet]
    assert drawing_part.DrawingSheets.CurrentDrawingSheet is None and not drawing_part.IsModified


def test_save_keeps_an_open_drawing_and_a_disabled_preference(nx, drawing_part):
    drawing_part.DrawingSheets.CurrentDrawingSheet = drawing_part.sheet
    assert call(nx, tools.nx_save_part, part=nx.ref_of(drawing_part, "part")).ok
    assert drawing_part.DrawingSheets.CurrentDrawingSheet is drawing_part.sheet
    drawing_part.DrawingSheets.CurrentDrawingSheet = None
    drawing_part.SaveOptions.DrawingCgmData = False
    opened = drawing_part.sheet.opened
    assert (
        call(nx, tools.nx_save_part, part=nx.ref_of(drawing_part, "part")).ok
        and drawing_part.sheet.opened == opened
    )


def test_save_error_still_restores_the_display(nx, drawing_part):
    nx.save_error = NXException(1, "disk error")
    bad = call(nx, tools.nx_save_part, part=nx.ref_of(drawing_part, "part"))
    assert bad.error["code"] == "NX_SAVE_FAILED" and bad.mutation_outcome == "partial"
    assert drawing_part.DrawingSheets.CurrentDrawingSheet is None


def test_a_failed_display_restore_is_partial(nx, drawing_part):
    from types import SimpleNamespace as NS

    def broken():
        raise RuntimeError("restore failure")

    drawing_part.Drafting = NS(ExitDraftingApplication=broken)
    bad = call(nx, tools.nx_save_part, part=nx.ref_of(drawing_part, "part"))
    assert bad.error["code"] == "NX_RESTORE_FAILED" and bad.mutation_outcome == "partial"


def test_an_active_sketch_is_refused_before_the_sheet_opens(nx, drawing_part):
    nx.session.ActiveSketch = object()
    bad = call(nx, tools.nx_save_part, part=nx.ref_of(drawing_part, "part"))
    assert bad.error["code"] == "NX_SKETCH_ACTIVE" and drawing_part.sheet.opened == 0


def test_save_as_and_close_use_the_drawing_context(nx, drawing_part):
    ref = nx.ref_of(drawing_part, "part")
    assert call(nx, tools.nx_save_part, part=ref, path="copy/dwg2.prt").ok
    assert drawing_part.DrawingSheets.CurrentDrawingSheet is None
    assert call(nx, tools.nx_close_part, part=ref, save=True).ok
    assert drawing_part.sheet.opened == 2 and drawing_part not in nx.session.Parts


@pytest.mark.parametrize("path", [None, "copy/dwg3.prt"])
def test_save_updates_out_of_date_views_first_and_reports_them(nx, drawing_part, path):
    from types import SimpleNamespace as NS

    from tests.kit.objects import Object

    stale, fresh = Object("ORTHO@1"), Object("ORTHO@2")
    stale.IsOutOfDate, fresh.IsOutOfDate = True, False
    drawing_part.sheet.GetDraftingViews = lambda: [stale, fresh]
    seen = []

    def update(views):
        seen.append([v.Name for v in views])
        for v in views:
            v.IsOutOfDate = False

    saved = []
    real = type(drawing_part).Save
    drawing_part.Save = lambda *a: (saved.append(stale.IsOutOfDate), real(drawing_part, *a))[1]
    drawing_part.DraftingViews = NS(UpdateViews=update)
    drawing_part.SaveOptions.DrawingCgmData = False  # out-of-date views alone open the sheet
    ok = call(
        nx,
        tools.nx_save_part,
        part=nx.ref_of(drawing_part, "part"),
        **({"path": path} if path else {}),
    )
    assert ok.ok and seen == [["ORTHO@1"]], ok
    assert [r["id"] for r in ok.data["updated_views"]] == [nx.ref_of(stale, "drawing_view")]
    assert "Updated 1 out-of-date drawing view(s)" in ok.warnings[-1]["message"]
    assert saved in ([False], [])  # updated before the native save (save-as: SaveAs)
    assert drawing_part.DrawingSheets.CurrentDrawingSheet is None
    again = call(nx, tools.nx_save_part, part=nx.ref_of(drawing_part, "part"))
    assert again.ok and "updated_views" not in again.data and len(seen) == 1


def test_saving_the_work_part_never_saves_its_components(nx):
    part = nx.new_part("asm.prt")
    calls = []
    real = type(part).Save

    def save(self, components, close):
        calls.append((components, close))
        return real(self, components, close)

    part.Save = save.__get__(part)
    assert call(nx, tools.nx_save_part, part=nx.ref_of(part, "part")).ok
    assert calls == [(False, False)]  # SaveComponents.FalseValue, CloseAfterSave.FalseValue


# ---- save audit: exactly the part's own file is saved ----
def modified_child(nx, rel="child.prt"):
    work = nx.session.Parts.Work
    child = nx.new_part(rel)
    child.Save()
    child.IsModified = True
    nx.session.Parts.Work = nx.session.Parts.Display = work
    return child


def test_save_leaves_other_modified_parts_alone(nx, root):
    top = nx.new_part("top.prt")
    child = modified_child(nx)
    before = (root / "child.prt").read_bytes()
    nx.session.Parts.Work = nx.session.Parts.Display = nx.session.Parts[-2]
    ok = call(nx, tools.nx_save_part, part=nx.ref_of(top, "part"))
    assert ok.data["saved_files"] == ["top.prt"]
    others = ok.data["other_modified_parts"]
    assert [(r["id"], r["kind"], r["name"]) for r in others] == [
        (nx.ref_of(child, "part"), "part", child.Name)
    ]
    assert child.IsModified and (root / "child.prt").read_bytes() == before
    child.IsModified = False
    top.IsModified = True
    clean = call(nx, tools.nx_save_part, part=nx.ref_of(top, "part"))
    assert clean.ok and clean.data["other_modified_parts"] == []
    assert clean.data["saved"] is True


def test_save_leaves_an_unmodified_part_as_it_is(nx, root):
    top = nx.new_part("top.prt")
    top.Save()
    before = (root / "top.prt").stat().st_mtime_ns, (root / "top.prt").read_bytes()
    marks = dict(nx.session.marks)
    same = call(nx, tools.nx_save_part, part=nx.ref_of(top, "part"))
    assert same.ok and same.mutation_outcome == "not_applicable", same
    assert same.data["saved"] is False and same.data["saved_files"] == []
    assert [w["code"] for w in same.warnings] == ["NX_NO_EFFECT"]
    assert ((root / "top.prt").stat().st_mtime_ns, (root / "top.prt").read_bytes()) == before
    assert dict(nx.session.marks) == marks  # undo marks kept


def test_save_as_onto_an_existing_file_suggests_a_free_path(nx, root):
    top = nx.new_part("top.prt")
    top.Save()
    (root / "copy.prt").write_bytes(b"there")
    taken = call(nx, tools.nx_save_part, part=nx.ref_of(top, "part"), path="copy.prt")
    assert taken.error["code"] == "NX_ALREADY_EXISTS", taken
    assert taken.error["details"] == {"path": "copy.prt", "suggested_path": "copy-2.prt"}
    assert "copy-2.prt" in taken.error["hint"]


def test_a_flag_flipped_on_another_part_is_partial(nx):
    top = nx.new_part("top.prt")
    child = modified_child(nx)
    nx.session.Parts.Work = nx.session.Parts.Display = top
    real = type(top).Save

    def faulty(self, *args):
        child.IsModified = False
        return real(self, *args)

    top.Save = faulty.__get__(top)
    bad = call(nx, tools.nx_save_part, part=nx.ref_of(top, "part"))
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "partial"
    assert bad.error["details"]["unexpected_changes"] == ["child.prt"]
    assert bad.error["details"]["saved_files"] == ["top.prt"]
    assert [r["name"] for r in bad.error["details"]["other_modified_parts"]] == [child.Name]


def test_native_unsaved_objects_are_not_a_success(nx):
    from types import SimpleNamespace as NS

    part = nx.new_part("u.prt")
    disposed = []
    status = NS(NumberUnsavedParts=1, NumberUnsavedObjects=0, GetPart=lambda i: part,
                GetStatus=lambda i: 123, Dispose=lambda: disposed.append(1))  # fmt: skip
    part.Save = lambda *a: status
    bad = call(nx, tools.nx_save_part, part=nx.ref_of(part, "part"))
    assert bad.error["code"] == "NX_VERIFICATION_FAILED"
    assert bad.error["details"]["native_save_errors"] == [{"path": part.FullPath, "nx_code": 123}]
    assert not bad.error["details"]["saved_files"] and disposed == [1]


def test_an_unreadable_loaded_file_stops_the_save_before_nx(nx, monkeypatch):
    from nx_mcp.domains.parts_files import _saving

    part = nx.new_part("p.prt")
    saves = []
    part.Save = lambda *a: saves.append(a)

    def denied(path):
        raise PermissionError("denied")

    monkeypatch.setattr(_saving, "fingerprint", denied)
    bad = call(nx, tools.nx_save_part, part=nx.ref_of(part, "part"))
    assert bad.error["code"] == "NX_SAVE_FAILED" and bad.mutation_outcome == "not_started"
    assert saves == []


def test_open_reuses_a_loaded_part_spelled_differently(nx, root):
    part = nx.new_part("project/base.prt")
    part.Save()
    part.FullPath = str(root / "project" / "sub" / ".." / "base.prt")
    ok = call(nx, tools.nx_open_part, path="project/base.prt")
    assert ok.data["already_loaded"] and nx.opened_files == []


def test_open_part_loads_components_as_saved_and_lists_the_ones_nx_cannot_find(nx, root):
    """A component saved in a subfolder: NX looks only in the assembly's folder when the
    session loads components from there (status 641044). The open loads them as saved,
    lists what NX still could not load and succeeds."""
    from types import SimpleNamespace as NS

    blk = nx.new_part("p01/sub/blk.prt")
    blk.Save()
    asm = nx.new_part("p01/asm.prt")
    nx.attach(asm, blk)
    asm.Save()
    asm.Close()
    nx.structure[str((root / "p01/asm.prt").resolve()).casefold()] = [str(root / "p01/sub/blk.prt")]
    opened = nx.session.Parts.OpenBase
    methods = []

    def open_base(path):
        methods.append(nx.session.Parts.LoadOptions.ComponentLoadMethod)
        part, _status = opened(path)
        status = NS(NumberUnloadedParts=1, GetPartName=lambda i: "blk.prt",
                    GetStatus=lambda i: 641044, Dispose=lambda: None)  # fmt: skip
        return part, status

    nx.session.Parts.OpenBase = open_base
    ok = call(nx, tools.nx_open_part, path="p01/asm.prt")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert methods == ["as_saved"] and nx.session.Parts.LoadOptions.ComponentLoadMethod == "search"
    assert ok.data["unloaded_components"] == [
        {"file": "blk.prt", "nx_code": 641044, "reason": tools.parts.LOAD_STATUS[641044]}
    ]
    assert [w["code"] for w in ok.warnings if "unloaded_components" in w["message"]] == [
        "NX_UPDATE_WARNING"
    ]


def test_open_part_with_load_components_succeeds_and_lists_unloaded_ones(nx, root):
    from types import SimpleNamespace as NS

    blk = nx.new_part("p02/sub/blk.prt")
    blk.Save()
    asm = nx.new_part("p02/asm.prt")
    nx.attach(asm, blk)
    asm.Save()
    asm.Close()
    nx.structure[str((root / "p02/asm.prt").resolve()).casefold()] = [str(root / "p02/sub/blk.prt")]
    ok = call(nx, tools.nx_open_part, path="p02/asm.prt", display=False)
    assert ok.ok and "unloaded_components" not in ok.data, ok
    part = next(p for p in nx.session.Parts if p.Name == "asm")
    proto = next(p for p in nx.session.Parts if p.Name == "blk")
    proto.IsFullyLoaded = False
    failing = NS(NumberUnloadedParts=1, GetPartName=lambda i: "blk.prt",
                 GetStatus=lambda i: 641044, Dispose=lambda: None)  # fmt: skip
    part.ComponentAssembly.OpenComponents = lambda option, comps: (failing, None)
    again = call(nx, tools.nx_open_part, path="p02/asm.prt", load_components=True, display=False)
    assert again.ok and again.data["components_loaded"] == 1, again
    assert again.data["unloaded_components"][0]["component"] == "BLK"
    assert again.data["unloaded_components"][0]["nx_code"] == 641044


def test_activate_that_changes_nothing_says_so(nx):
    a, b = nx.new_part("a.prt"), nx.new_part("b.prt")
    ref = nx.ref_of(a, "part")
    first = call(nx, tools.nx_activate_part, part=ref)  # gives a its own tab behind b's
    assert first.ok
    again = call(nx, tools.nx_activate_part, part=ref)  # its tab exists: nothing changes
    assert again.ok and again.mutation_outcome == "not_applicable", again
    (warning,) = [w for w in again.warnings if w["code"] == "NX_NO_EFFECT"]
    assert "only display true" in warning["message"] and '"part": "a.prt"' in warning["message"]
    assert nx.session.Parts.Display is b
    shown = call(nx, tools.nx_activate_part, part=ref, display=True)
    assert shown.ok and shown.mutation_outcome == "committed" and shown.data["display"]


def test_unknown_part_errors_list_the_callers_parts_first_with_the_count(nx, root):
    for i in range(12):
        nx.new_part(f"many/p{i:02d}.prt")
    mine = call(nx, tools.nx_create_part, path="many/mine.prt", display=False)
    services(nx).foreign.update(
        services(nx).part_key(p) for p in nx.session.Parts if p.Name.startswith("p")
    )
    bad = call(nx, tools.nx_activate_part, part="nothing.prt")
    assert bad.error["code"] == "NX_NOT_FOUND", bad
    details = bad.error["details"]
    assert details["loaded_count"] == len(nx.session.Parts) and len(details["loaded_parts"]) == 10
    assert mine.data["part"]["id"] in [r["id"] for r in details["loaded_parts"][:2]]  # own first


def test_create_part_reports_the_new_parts_units_when_not_shown(nx, root):
    ok = call(nx, tools.nx_create_part, path="u/inch_quiet.prt", units="in", display=False)
    assert ok.ok and not ok.data["work"] and ok.units["length"] == "in", ok


# ---- usability: units block, part refs, save to the part's own file ----
def test_the_units_block_follows_the_part_the_call_activates(nx):
    inch = call(nx, tools.nx_create_part, path="u/inch1.prt", units="in", display=True)
    assert inch.units["length"] == "in" and inch.data["units"] == "in"
    mm = call(nx, tools.nx_create_part, path="u/mm1.prt", display=True)
    assert mm.units["length"] == "mm" and mm.data["units"] == "mm"
    back = call(nx, tools.nx_activate_part, part=inch.data["part"]["id"], display=True)
    assert back.units["length"] == "in"
    listed = call(nx, tools.nx_list_parts, active_only=True)
    assert listed.units["length"] == listed.data["items"][0]["units"] == "in"
    closed = call(nx, tools.nx_close_part, part=inch.data["part"]["id"], save=False)
    work = nx.session.Parts.Work
    expected = "mm" if work is not None and work.PartUnits == "mm" else None
    assert (closed.units or {}).get("length") == expected


def test_a_stale_part_ref_points_to_nx_list_parts_and_the_current_refs(nx, root):
    part = nx.new_part("re/open1.prt")
    part.Save()
    old = nx.ref_of(part, "part")
    assert call(nx, tools.nx_close_part, part=old, save=False).ok
    reopened = call(nx, tools.nx_open_part, path="re/open1.prt")
    assert reopened.ok and reopened.data["part"]["id"] != old
    for fn, args in [(tools.nx_activate_part, {}), (tools.nx_save_part, {}),
                     (tools.nx_close_part, {"save": False})]:  # fmt: skip
        stale = call(nx, fn, part=old, **args)
        assert stale.error["category"] == "stale_reference", stale
        assert stale.mutation_outcome == "not_started" and stale.error["field"] == "/part"
        assert "nx_list_parts" in stale.error["hint"]
        rows = {r["id"]: r for r in stale.error["details"]["loaded_parts"]}
        assert rows[reopened.data["part"]["id"]]["path"] == "re/open1.prt"
    unknown = call(nx, tools.nx_activate_part, part="p:999999")
    assert "nx_list_parts" in unknown.error["hint"] and unknown.error["details"]["loaded_parts"]


def test_part_fields_say_where_refs_and_paths_come_from():
    from nx_mcp.sidecar import schema

    for fn in (tools.nx_activate_part, tools.nx_save_part, tools.nx_close_part):
        prop = schema.input_schema(fn.spec)["properties"]["part"]
        assert "nx_list_parts" in prop["description"], fn
    for fn in (tools.nx_create_part, tools.nx_open_part, tools.nx_save_part):
        assert ".prt" in schema.input_schema(fn.spec)["properties"]["path"]["description"]


def test_save_to_the_parts_own_file_saves_in_place(nx, root):
    part = nx.new_part("own/plate1.prt")  # created, never written
    part.IsModified = True
    ref = nx.ref_of(part, "part")
    first = call(nx, tools.nx_save_part, part=ref, path="own/plate1.prt")
    assert first.ok and first.mutation_outcome == "committed", first
    assert "saved_as" not in first.data and first.data["saved_files"] == ["own/plate1.prt"]
    assert (root / "own" / "plate1.prt").is_file() and not part.IsModified
    part.IsModified = True
    again = call(
        nx, tools.nx_save_part, part=ref, path="OWN/Plate1.prt"
    )  # the work part's file exists
    assert again.ok and again.data["saved_files"] == ["own/plate1.prt"], again
    assert again.data["part"]["id"] == ref


def test_a_refused_save_as_that_changed_nothing_is_not_partial(nx, root):
    part = nx.new_part("keep.prt")
    ref = nx.ref_of(part, "part")
    before = part.FullPath

    def refuse(path):
        raise NXException(1020004, "File already exists")

    part.SaveAs = refuse
    bad = call(nx, tools.nx_save_part, part=ref, path="deep/new/keep_v2.prt")
    assert bad.error["code"] == "NX_SAVE_FAILED" and bad.error["nx_code"] == 1020004
    assert bad.mutation_outcome == "rolled_back", bad
    assert "omit path" in bad.error["hint"] and part.FullPath == before
    assert not (root / "deep").exists()  # the folders the call made are gone again


def test_a_save_as_failure_after_writing_is_partial(nx, root):
    part = nx.new_part("half.prt")

    def half(path):
        Path(path).write_bytes(b"half")
        raise NXException(7, "Disk full")

    part.SaveAs = half
    bad = call(nx, tools.nx_save_part, part=nx.ref_of(part, "part"), path="half_v2.prt")
    assert bad.error["code"] == "NX_SAVE_FAILED" and bad.mutation_outcome == "partial"


def test_unsaved_part_refusal_names_the_part(nx):
    dirty = nx.new_part("d/dirty2.prt")
    dirty.IsModified = True
    ref = nx.ref_of(dirty, "part")
    refused = call(nx, tools.nx_close_part, part=ref)
    assert refused.error["code"] == "NX_UNSAVED_PART"
    details = refused.error["details"]
    assert details["part"]["id"] == ref and details["path"] == "d/dirty2.prt"
    assert details["modified"] is True


def test_close_part_by_workspace_path_or_file_name(nx, root):
    by_path = nx.new_part("cl/by_path.prt")  # created, never saved: no file yet
    ok = call(nx, tools.nx_close_part, part="cl/by_path.prt", save=False)
    assert ok.ok and ok.data["closed_count"] == 1 and by_path not in nx.session.Parts, ok
    absolute = nx.new_part("cl/absolute.prt")
    assert call(nx, tools.nx_close_part, part=str(root / "cl" / "absolute.prt")).ok
    assert absolute not in nx.session.Parts
    for name in ("By_Name.prt", "by_name"):  # a bare file name, with or without .prt
        part = nx.new_part("cl/deep/by_name.prt")
        closed = call(nx, tools.nx_close_part, part=name)
        assert closed.ok and part not in nx.session.Parts, closed
        assert closed.data["saved"] is False  # never saves by default


def test_close_part_by_path_keeps_the_refusals(nx, root):
    dirty = nx.new_part("cl/dirty.prt")
    dirty.IsModified = True
    refused = call(nx, tools.nx_close_part, part="cl/dirty.prt")
    assert refused.error["code"] == "NX_UNSAVED_PART" and dirty in nx.session.Parts
    missing = call(nx, tools.nx_close_part, part="cl/other.prt", save=False)
    assert missing.error["code"] == "NX_NOT_FOUND" and missing.error["field"] == "/part"
    assert missing.mutation_outcome == "not_started"
    assert "nx_list_parts" in missing.error["hint"]
    assert {r["path"] for r in missing.error["details"]["loaded_parts"]} >= {"cl/dirty.prt"}
    deeper = call(nx, tools.nx_close_part, part="elsewhere/dirty.prt", save=False)
    assert deeper.error["code"] == "NX_NOT_FOUND"  # a path names that file only
    outside = call(nx, tools.nx_close_part, part="../dirty.prt", save=False)
    assert outside.error["code"] == "NX_PATH_OUTSIDE_WORKSPACE"
    svc = services(nx)
    svc.dev = False
    svc.owns = lambda part: False
    granted = call(nx, tools.nx_close_part, part="dirty", save=False)
    assert granted.error["code"] == "NX_ENGINEER_PART" and dirty in nx.session.Parts
    assert granted.error["category"] == "blocked" and granted.mutation_outcome == "not_started"


def test_close_part_summary_names_the_file():
    from nx_mcp.core import summary

    spec = tools.nx_close_part.spec
    text = summary.render(spec, {"part": "C:/ws/cl/plate.prt", "save": True}, strict=True)
    assert text == "Close plate.prt, saving it"


# ---- closing an assembly closes the unused component parts the agent loaded ----
def _keeps_prototypes(nx, part):
    """NX 2406 keeps a part loaded on its own (created or opened) after its assembly
    closes: only the assembly itself unloads."""
    from tests.kit.objects import Part

    part.Close = lambda *_a: Part.Close(part)
    part.CloseRetainUndoMarks = lambda *_a: Part.CloseRetainUndoMarks(part)


def test_close_assembly_unloads_unused_agent_component_parts(nx):
    blk = nx.new_part("kit/blk.prt")
    pin = nx.new_part("kit/pin.prt")
    sub = nx.new_part("kit/sub.prt")
    nx.attach(sub, pin)
    asm = nx.new_part("kit/asm.prt")
    nx.attach(asm, blk)
    nx.attach(asm, sub)
    for part in (asm, sub):
        _keeps_prototypes(nx, part)
    ok = call(nx, tools.nx_close_part, part=nx.ref_of(asm, "part"), save=False)
    assert ok.ok, ok
    assert {r["name"] for r in ok.data["closed"]} == {"asm", "blk", "sub", "pin"}
    assert {r["name"] for r in ok.data["unloaded_components"]} == {"blk", "sub", "pin"}
    left = list(nx.session.Parts)
    assert ok.data["closed_count"] == 4 and ok.data["remaining_count"] == len(left)
    assert "kept_components" not in ok.data and not {blk, pin, sub, asm} & set(left)


def test_close_assembly_keeps_modified_shared_active_and_foreign_component_parts(nx):
    mine = nx.new_part("kit/mine.prt")
    dirty = nx.new_part("kit/dirty.prt")
    dirty.IsModified = True
    shared = nx.new_part("kit/shared.prt")
    theirs = nx.new_part("kit/theirs.prt")
    other = nx.new_part("kit/other.prt")
    nx.attach(other, shared)
    asm = nx.new_part("kit/asm.prt")
    for proto in (mine, dirty, shared, theirs):
        nx.attach(asm, proto)
    _keeps_prototypes(nx, asm)
    svc = services(nx)
    svc.created = lambda part: part is not theirs
    nx.session.Parts.Work = nx.session.Parts.Display = mine  # active before the call
    ok = call(nx, tools.nx_close_part, part=nx.ref_of(asm, "part"), save=False)
    assert ok.ok, ok
    kept = {r["part"]["name"]: r["reason"] for r in ok.data["kept_components"]}
    assert set(kept) == {"mine", "dirty", "shared", "theirs"}
    assert "unsaved changes" in kept["dirty"] and "another loaded assembly" in kept["shared"]
    assert "not created" in kept["theirs"] and "work or display" in kept["mine"]
    assert "unloaded_components" not in ok.data and ok.data["closed_count"] == 1
    assert dirty in nx.session.Parts and dirty.IsModified  # never discarded implicitly


def test_close_assembly_with_unload_components_false_keeps_them(nx):
    blk = nx.new_part("kit/blk.prt")
    asm = nx.new_part("kit/asm.prt")
    nx.attach(asm, blk)
    _keeps_prototypes(nx, asm)
    ok = call(nx, tools.nx_close_part, part=nx.ref_of(asm, "part"), save=False,
              unload_components=False)  # fmt: skip
    assert ok.ok and ok.data["closed_count"] == 1 and blk in nx.session.Parts
    assert "unloaded_components" not in ok.data and "kept_components" not in ok.data

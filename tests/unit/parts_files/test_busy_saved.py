"""The busy rule after a save (core/busy.py, benchmark bench3p): a part whose changes are
saved (or that was just opened from its file) is not busy, so an agent's own close after
its own save closes even when another agent's call came last; a change after the save
makes it busy again. Exports never change a part. Also: nx_open_part reports a part NX
marked modified while loading it."""

from __future__ import annotations

from types import SimpleNamespace as NS

from nx_mcp.core import busy
from nx_mcp.domains.parts_files import parts as tools
from tests.kit.kernel import services
from tests.unit.parts_files.support import call
from tests.unit.parts_files.test_close_multipart import _tools, block, create  # noqa: F401


def test_a_saved_part_closes_although_another_part_got_the_last_call(nx):
    create(nx, "mc/a.prt")
    create(nx, "mc/b.prt", display=False)
    block(nx, "a")
    assert call(nx, tools.nx_save_part, part="a").ok
    block(nx, "b")  # another agent's call comes last
    closed = call(nx, tools.nx_close_part, part="a", save=False)
    assert closed.ok, closed


def test_a_change_after_the_save_makes_the_part_busy_again(nx):
    create(nx, "mc/a.prt")
    create(nx, "mc/b.prt", display=False)
    assert call(nx, tools.nx_save_part, part="a").ok
    block(nx, "a")
    block(nx, "b")
    refused = call(nx, tools.nx_close_part, part="a", save=False)
    assert refused.error["code"] == "NX_PART_BUSY", refused
    assert refused.error["details"]["last_change"]["tool"] == "nx_create_test_block"
    assert "save it first" in refused.error["hint"]


def test_a_close_that_saves_still_respects_another_streams_unsaved_changes(nx):
    create(nx, "mc/a.prt")
    create(nx, "mc/b.prt", display=False)
    block(nx, "a")
    block(nx, "b")
    refused = call(nx, tools.nx_close_part, part="a", save=True)
    assert refused.error["code"] == "NX_PART_BUSY", refused


def test_file_calls_never_change_a_part_and_a_save_marks_it_saved():
    svc = NS(part_activity=lambda table={}: table)  # noqa: B006
    target = NS(key="part_x")
    busy.record(svc, NS(effect="model", name="nx_extrude"), target, None, None, now=100.0)
    busy.record(svc, NS(effect="file", name="nx_save_part"), target, None, None, now=110.0)
    entry = svc.part_activity()["parts"]["part_x"]
    assert entry["changed_at"] == 100.0 and entry["saved_at"] == 110.0
    assert busy._unsaved_age(entry, 120.0) is None
    busy.record(svc, NS(effect="file", name="nx_export_model"), target, None, None, now=130.0)
    assert entry["changed_at"] == 100.0 and entry["at"] == 130.0  # worked in, not changed
    assert busy._unsaved_age(entry, 140.0) is None
    busy.record(svc, NS(effect="model", name="nx_blend"), target, None, None, now=150.0)
    assert busy._unsaved_age(entry, 160.0) == 10.0
    assert entry["change"]["tool"] == "nx_blend"
    assert busy._unsaved_age(entry, 150.0 + busy.BUSY_SECONDS + 1) is None


def test_the_save_counts_as_work_for_an_assembly_close(nx):
    svc = services(nx)
    target = NS(key="comp")
    busy.record(svc, NS(effect="file", name="nx_save_part"), target, None, None, now=200.0)
    entry = svc.part_activity()["parts"]["comp"]
    assert entry["at"] == 200.0 and entry["tool"] == "nx_save_part"


def test_a_part_nx_marks_modified_while_loading_it_is_reported_not_refused(nx, root):
    a = nx.new_part("kit.prt")
    a.Save()
    a.Close()
    plain = nx.session.Parts.OpenBase

    def open_updated(path):  # NX updates the assembly on load
        part, status = plain(path)
        part.IsModified = True
        return part, status

    nx.session.Parts.OpenBase = open_updated
    opened = call(nx, tools.nx_open_part, path="kit.prt", load_components=True)
    assert opened.ok and opened.data["modified_on_load"] is True, opened
    note = next(w["message"] for w in opened.warnings if "while loading" in w["message"])
    assert note.startswith("NX marked kit.prt modified") and "changed nothing" in note
    clean = nx.new_part("clean.prt")
    clean.Save()
    clean.Close()
    nx.session.Parts.OpenBase = plain
    fresh = call(nx, tools.nx_open_part, path="clean.prt")
    assert fresh.ok and "modified_on_load" not in fresh.data


def test_a_part_just_opened_from_its_file_is_not_busy(nx, root):
    create(nx, "mc/a.prt")
    block(nx, "a")
    assert call(nx, tools.nx_save_part, part="a").ok
    assert call(nx, tools.nx_close_part, part="a", save=False).ok
    create(nx, "mc/b.prt", display=False)
    assert call(nx, tools.nx_open_part, path="mc/a.prt").ok
    block(nx, "b")  # another agent's call comes last
    closed = call(nx, tools.nx_close_part, part="a", save=False)
    assert closed.ok, closed


def test_reusing_a_loaded_part_keeps_its_unsaved_changes_busy(nx):
    create(nx, "mc/a.prt")
    create(nx, "mc/b.prt", display=False)
    assert call(nx, tools.nx_save_part, part="a").ok  # its file exists
    block(nx, "a")
    reused = call(nx, tools.nx_open_part, path="mc/a.prt", work=False, display=False)
    assert reused.ok and reused.data["already_loaded"], reused
    block(nx, "b")
    refused = call(nx, tools.nx_close_part, part="a", save=False)
    assert refused.error["code"] == "NX_PART_BUSY", refused

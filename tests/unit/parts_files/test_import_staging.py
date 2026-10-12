"""The import staging folders (domains/parts_files/importing.py): a redo of an import
reuses its finished folder (bench: nx_redo_operation of an import failed with
FileExistsError WinError 183 on imports/<operation_id>), a folder still in use is never
reused, and finished folders are removed by later imports (bench: 487 left over)."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace as NS

from nx_mcp.core import catalogue
from nx_mcp.domains.parts_files import importing
from tests.unit.parts_files.support import call
from tests.unit.parts_files.test_exchange import STEP

UNDO = NS(spec=catalogue.load()["nx_undo_operation"])
REDO = NS(spec=catalogue.load()["nx_redo_operation"])


def imported(nx, operation_id="op_import_redo_1"):
    ok = call(nx, importing.nx_import_model, part="w", path="in.step", format="step",
              operation_id=operation_id)  # fmt: skip
    assert ok.ok, ok
    return ok


def test_a_redo_of_an_import_reuses_its_finished_staging_folder(nx, root, ugii):
    part = nx.new_part("w.prt")
    (root / "in.step").write_text(STEP)
    first = imported(nx)
    folder = root / "imports" / "op_import_redo_1"
    assert first.data["staging"] == "imports/op_import_redo_1" and folder.is_dir()
    assert (folder / "input.step").is_file()
    undone = call(nx, UNDO, part="w")
    assert undone.ok and len(part.Bodies) == 0, undone
    redone = call(nx, REDO, part="w")
    assert redone.ok, redone  # was FileExistsError on imports/op_import_redo_1
    assert redone.data["redone"] == ["op_import_redo_1"] and len(part.Bodies) == 1
    assert redone.data["results"][0]["data"]["staging"] == "imports/op_import_redo_1"


def test_a_staging_folder_in_use_is_never_reused_and_finished_ones_are_swept(nx, root, ugii):
    nx.new_part("w.prt")
    (root / "in.step").write_text(STEP)
    base = root / "imports"
    busy = base / "op_import_busy_1"
    busy.mkdir(parents=True)
    (busy / "input.step").write_text(STEP)
    (busy / "blk.prt").write_text("an unflattened assembly's prototype")
    first = imported(nx, "op_import_busy_1")
    assert first.data["staging"] == "imports/op_import_busy_1-2", first.data
    assert (busy / "blk.prt").is_file()  # kept: it holds a part file
    old = time.time() - importing.STAGING_KEEP_SECONDS - 60
    finished = base / "op_old_import_1"
    finished.mkdir()
    for name in ("input.stl", "output.log"):
        (finished / name).write_text("x")
    user = base / "vendor_files"  # not a staging folder: a user's own files
    user.mkdir()
    (user / "bracket.step").write_text(STEP)
    recent = base / "op_recent_import_1"
    recent.mkdir()
    (recent / "input.igs").write_text("x")
    for folder in (finished, user, busy):
        os.utime(folder, (old, old))
    imported(nx, "op_import_sweep_1")
    assert not finished.exists()  # finished and old: removed
    assert user.exists() and recent.exists() and busy.exists()
    assert (base / "op_import_sweep_1").is_dir()  # this import's own folder stays

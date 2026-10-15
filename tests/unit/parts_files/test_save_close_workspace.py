"""nx_save_part / nx_close_part on the host: parts outside the workspace are refused
before NX saves or closes anything."""

from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("nx_save_part", {}),
        ("nx_save_part", {"path": "copy.prt"}),
        ("nx_close_part", {"part": "WORK"}),
    ],
)
def test_save_and_close_refuse_parts_outside_the_workspace(
    rig, tmp_path, method, params, monkeypatch
):
    from nx_mcp.core import kernel
    from nx_mcp.host.dispatch import KernelError

    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)
    outside = tmp_path.parent / f"{tmp_path.name}-engineer" / "bracket.prt"
    rig.part.FullPath = str(outside)
    rig.part.Save = Mock()
    rig.part.SaveAs = Mock()
    rig.part.Close = Mock()
    if params.get("part") == "WORK":  # nx_close_part names the part by its Ref
        params = {"part": rig.e.register(rig.part, "part", rig.part, "Part")["id"]}
    with pytest.raises(KernelError) as error:
        rig.run(method, params)
    assert error.value.code == "NX_PATH_OUTSIDE_WORKSPACE"
    assert error.value.details["mutation_outcome"] == "not_started"
    assert error.value.envelope["error"]["details"]["path"] == str(outside)
    rig.part.Save.assert_not_called()
    rig.part.SaveAs.assert_not_called()
    rig.part.Close.assert_not_called()
    assert not outside.exists() and not (tmp_path / "copy.prt").exists()


def test_unsaved_part_without_a_file_may_only_be_saved_as_into_the_workspace(
    rig, tmp_path, monkeypatch
):
    from nx_mcp.core import kernel
    from nx_mcp.host.dispatch import KernelError

    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)
    rig.part.FullPath = "model1"
    rig.e.own(rig.part)  # a new part the agent created (gate 6)
    ref = rig.e.register(rig.part, "part", rig.part, "Part")["id"]
    for method, params in [("nx_save_part", {}), ("nx_close_part", {"part": ref})]:
        with pytest.raises(KernelError) as error:
            rig.run(method, params)
        assert error.value.code == "NX_PATH_OUTSIDE_WORKSPACE"
    with pytest.raises(KernelError) as error:
        rig.run("nx_save_part", {"path": str(tmp_path.parent / "elsewhere.prt")})
    assert error.value.code == "NX_PATH_OUTSIDE_WORKSPACE"
    destination = tmp_path / "smoke" / "model1.prt"

    def save_as(path):
        Path(path).write_bytes(b"saved")
        rig.part.FullPath = str(path)
        return NS(Dispose=Mock())

    rig.part.SaveAs = save_as
    saved_as = rig.run("nx_save_part", {"path": str(destination)})
    assert saved_as["data"]["path"] == "smoke/model1.prt"
    rig.part.IsModified = True  # an unmodified part is left as it is
    assert rig.run("nx_save_part", {})["data"]["saved_files"] == ["smoke/model1.prt"]

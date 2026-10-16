"""Gate 6 on the host: part ownership, part tracking and the panel grant."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from nx_mcp.core import kernel
from nx_mcp.core.errors import NXError
from nx_mcp.host import dispatch
from tests.kit.objects import Body, Part


@pytest.fixture
def engineer(rig, monkeypatch):
    """Production mode with the work part belonging to the engineer."""
    monkeypatch.delenv("NX_MCP_DEV", raising=False)
    rig.e.owned_parts.clear()
    rig.services = rig.e
    return rig


def _load(rig, path):
    """A part the engineer has loaded besides the work part."""
    part = Part(rig.session, path)  # the fake makes it work and display part
    rig.session.Parts.Work = rig.session.Parts.Display = rig.part
    return part


def _edit(rig, part=None):
    """A development tool that adds a body to ``part`` (default: the work part)."""

    def handler():
        target = part or rig.part
        target.Bodies.append(Body("B"))
        target.IsModified = True
        return {}

    rig.e.dev_tools["nx_test_edit"] = handler


def test_changes_are_refused_on_the_engineers_part_until_granted(engineer, monkeypatch):
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a, **k: None)
    engineer.part.Save = Mock(wraps=engineer.part.Save)
    with pytest.raises(NXError) as error:
        engineer.run("nx_save_part", {})
    assert error.value.code == "NX_ENGINEER_PART"  # core/busy.py, before gate 6
    assert error.value.details["mutation_outcome"] == "not_started"
    assert "NX MCP panel" in error.value.hint
    engineer.part.Save.assert_not_called()
    # The engineer ticks "Allow agent to change test" on the panel.
    assert dispatch.grant_state(engineer.e) == {"name": "test", "granted": False}
    assert dispatch.set_grant(engineer.e, True) == "test"
    assert dispatch.grant_state(engineer.e) == {"name": "test", "granted": True}
    assert engineer.run("nx_save_part", {})["ok"]
    dispatch.set_grant(engineer.e, False)
    with pytest.raises(NXError):
        engineer.run("nx_save_part", {})


def test_reads_still_run_on_the_engineers_part(engineer):
    assert engineer.run("nx_list_objects", {"kind": "body"})["ok"]


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("nx_save_part", {}),
        ("nx_save_part", {"path": "copy.prt"}),
        ("nx_close_part", {"part": "WORK"}),
    ],
)
def test_save_and_close_need_ownership(engineer, method, args, monkeypatch):
    # The parts_files tools refuse the engineer's parts (core/busy.py) before gate 6.
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a, **k: None)
    engineer.part.Save = Mock()
    engineer.part.SaveAs = Mock()
    engineer.part.Close = Mock()
    if args.get("part") == "WORK":  # nx_close_part names the part by its Ref
        args = {"part": engineer.e.register(engineer.part, "part", engineer.part, "Part")["id"]}
    with pytest.raises(dispatch.KernelError) as error:
        engineer.run(method, args)
    assert error.value.code == "NX_ENGINEER_PART"
    assert error.value.details["mutation_outcome"] == "not_started"
    engineer.part.Save.assert_not_called()
    engineer.part.SaveAs.assert_not_called()
    engineer.part.Close.assert_not_called()


def test_close_checks_the_named_part_not_the_work_part(engineer, tmp_path, monkeypatch):
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a, **k: None)
    engineer.services.own(engineer.part)
    other = _load(engineer, tmp_path / "theirs.prt")
    other.Close = Mock()
    ref = engineer.e.register(other, "part", other, "Part")["id"]
    with pytest.raises(dispatch.KernelError) as error:
        engineer.run("nx_close_part", {"part": ref, "save": False})
    assert error.value.code == "NX_ENGINEER_PART" and "theirs.prt" in error.value.message
    other.Close.assert_not_called()


def test_development_mode_keeps_the_gate_and_the_grant(engineer, monkeypatch):
    """NX_MCP_DEV=1 never opens the engineer's parts to catalogue tools: gate 6 refuses
    a model change, busy refuses a save, and the panel shows the grant checkbox."""
    from nx_mcp.core import catalogue

    monkeypatch.setenv("NX_MCP_DEV", "1")
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a, **k: None)
    assert engineer.e.dev  # development mode
    model = next(s for s in catalogue.specs() if s.effect == "model" and s.new_part is None)
    with pytest.raises(NXError) as error:
        kernel._gate_ownership(model, engineer.e, {})
    assert error.value.code == "NX_PART_NOT_GRANTED"
    engineer.part.Save = Mock(wraps=engineer.part.Save)
    with pytest.raises(NXError) as error:
        engineer.run("nx_save_part", {})
    assert error.value.code == "NX_ENGINEER_PART"
    engineer.part.Save.assert_not_called()
    assert dispatch.grant_state(engineer.e) == {"name": "test", "granted": False}
    dispatch.set_grant(engineer.e, True)  # the engineer ticks the box in development too
    assert kernel._gate_ownership(model, engineer.e, {}) is not None  # passes: flags
    assert engineer.run("nx_save_part", {})["ok"]


def test_an_acceptance_like_run_beside_the_engineer_in_development(engineer, tmp_path, monkeypatch):
    """The acceptance runner's flow with the gate on: a part an engineer step (a
    development snippet) loads under smoke/ is the run's own, so catalogue calls change
    and save it; the engineer's own part stays refused until they tick the panel box."""
    monkeypatch.setenv("NX_MCP_DEV", "1")
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a, **k: None)
    made = []
    engineer.e.dev_tools["nx_test_create"] = lambda: (
        made.append(Part(engineer.session, tmp_path / "smoke" / "run" / "fixture.prt")) or {}
    )
    engineer.run("nx_test_create", {})
    fixture = made[0]
    assert engineer.e.created(fixture) and not engineer.e.owns(engineer.part)
    engineer.session.Parts.Work = engineer.session.Parts.Display = engineer.part
    fixture.Save = Mock(wraps=fixture.Save)
    ref = engineer.e.register(fixture, "part", fixture, "Part")["id"]
    assert engineer.run("nx_save_part", {"part": ref})["ok"]
    fixture.Save.assert_called_once()
    with pytest.raises(NXError) as error:
        engineer.run("nx_save_part", {"part": "test"})
    assert error.value.code == "NX_ENGINEER_PART"


def test_development_snippets_act_outside_the_gate(engineer, monkeypatch):
    """A development tool (nx_dev_python, the acceptance runner's engineer steps) is the
    developer's own action, not a catalogue call: it may edit the engineer's part."""
    monkeypatch.setenv("NX_MCP_DEV", "1")
    _edit(engineer)
    assert engineer.run("nx_test_edit", {})["status"] == "success"
    assert len(engineer.part.Bodies) == 1 and not engineer.e.owns(engineer.part)


def test_parts_the_agent_loads_are_owned_and_follow_its_save_as(engineer, tmp_path, monkeypatch):
    # Like nx_create_part: a tool that loads a new part and makes it work part.
    made = []
    engineer.e.dev_tools["nx_test_create"] = lambda: (
        made.append(Part(engineer.session, tmp_path / "made.prt")) or {}
    )
    engineer.run("nx_test_create", {})
    new = made[0]
    services = engineer.services
    assert services.created(new) and not services.owns(engineer.part)
    assert dispatch.grant_state(engineer.e) is None  # the agent's own part needs no grant

    def save_as():
        new.FullPath = str(tmp_path / "renamed.prt")
        return {}

    engineer.e.dev_tools["nx_test_save_as"] = save_as
    engineer.run("nx_test_save_as", {})
    assert services.owns(new)


def test_ownership_needs_the_same_file(engineer, tmp_path):
    services = engineer.services
    services.own(engineer.part)
    # NX may reuse a closed part's tag for another file: the key alone is not enough.
    engineer.part.FullPath = str(Path(tmp_path) / "someone-else.prt")
    assert not services.owns(engineer.part)
    services.grant(engineer.part)
    assert services.owns(engineer.part) and services.granted(engineer.part)
    services.revoke(engineer.part)
    assert not services.owns(engineer.part)


def test_parts_a_development_snippet_made_stay_the_agents_after_a_host_reload(
    engineer, tmp_path, monkeypatch
):
    """A part loaded by a call that records no owner (a development snippet) is still the
    agent's after ``dev_reload``: the host writes part-owners.json after tracking it, and
    the new host generation adopts it (same NX process, same tag and file)."""
    from nx_mcp.core import busy
    from nx_mcp.host.services import NXServices

    made = []
    engineer.e.dev_tools["nx_test_create"] = lambda: (
        made.append(Part(engineer.session, tmp_path / "made.prt")) or {}
    )
    engineer.run("nx_test_create", {})
    new = made[0]
    assert engineer.e.created(new)
    # A host reload: new services over the same NX session and workspace, nothing known.
    reloaded = NXServices(engineer.session, engineer.e.nx, engineer.e.build, engineer.e.workspace)
    assert not reloaded.created(new)
    reloaded.track(reloaded.loaded())  # the first call after the reload loads nothing
    assert reloaded.created(new) and not reloaded.created(engineer.part)
    assert busy.engineer(reloaded, engineer.part) and not busy.engineer(reloaded, new)
    # ... and a second reload still knows it (the new generation kept the row).
    again = NXServices(engineer.session, engineer.e.nx, engineer.e.build, engineer.e.workspace)
    busy.adopt(again)
    assert again.created(new)


def _reload(rig):
    """A development host reload: new services over the same NX session and workspace."""
    from nx_mcp.host.services import NXServices

    return NXServices(rig.session, rig.e.nx, rig.e.build, rig.e.workspace)


def test_a_panel_grant_survives_a_host_reload_in_the_same_nx_process(engineer):
    """The engineer ticked 'Allow agent to change' once: a development host reload keeps
    the part granted (part-owners.json, same pid, tag, file and name), before any call
    too (the panel's checkbox), and it stays the engineer's part (granted, not created)."""
    import json

    from nx_mcp.core import busy

    dispatch.set_grant(engineer.e, True)
    path = Path(engineer.e.workspace.root) / ".nx-mcp" / busy.OWNERS_FILE
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["granted"] == {
        str(engineer.part.Tag): {"file": busy._file(engineer.part), "name": "test"}
    }
    reloaded = _reload(engineer)
    assert dispatch.grant_state(reloaded) == {"name": "test", "granted": True}
    assert reloaded.owns(engineer.part) and not reloaded.created(engineer.part)
    assert busy.engineer(reloaded, engineer.part)
    # Withdrawn on the panel: the next reload does not grant it again.
    dispatch.set_grant(reloaded, False)
    assert dispatch.grant_state(_reload(engineer)) == {"name": "test", "granted": False}


def test_a_grant_is_forgotten_by_a_new_nx_process_or_a_new_file(engineer, tmp_path):
    import json

    from nx_mcp.core import busy

    dispatch.set_grant(engineer.e, True)
    path = Path(engineer.e.workspace.root) / ".nx-mcp" / busy.OWNERS_FILE
    saved = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**saved, "pid": -1}), encoding="utf-8")  # NX restarted
    assert not _reload(engineer).granted(engineer.part)
    path.write_text(json.dumps(saved), encoding="utf-8")
    assert _reload(engineer).granted(engineer.part)
    engineer.part.FullPath = str(tmp_path / "saved-as.prt")  # saved under another name
    assert not _reload(engineer).granted(engineer.part)


def test_an_adopted_grant_never_drops_the_agents_rows(engineer, tmp_path):
    """Restoring a grant during adoption writes nothing partial: the agent's own parts of
    the earlier host generation are still listed after the reload."""
    from nx_mcp.core import busy

    made = []
    engineer.e.dev_tools["nx_test_create"] = lambda: (
        made.append(Part(engineer.session, tmp_path / "made.prt")) or {}
    )
    engineer.run("nx_test_create", {})
    engineer.session.Parts.Work = engineer.session.Parts.Display = engineer.part
    dispatch.set_grant(engineer.e, True)
    reloaded = _reload(engineer)
    busy.adopt(reloaded)
    assert reloaded.created(made[0]) and reloaded.granted(engineer.part)
    busy.persist(reloaded)
    again = _reload(engineer)
    busy.adopt(again)
    assert again.created(made[0]) and again.granted(engineer.part)

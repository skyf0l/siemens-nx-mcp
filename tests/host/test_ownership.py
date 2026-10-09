"""Gate 6 on the host: part ownership, part tracking and the panel grant."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from nx_mcp.core import kernel
from nx_mcp.core.errors import NXToolError
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
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)
    engineer.part.Save = Mock(wraps=engineer.part.Save)
    with pytest.raises(NXToolError) as error:
        engineer.run("nx_save_part", {})
    assert error.value.code == "NX_PART_NOT_GRANTED"
    assert error.value.details["mutation_outcome"] == "not_started"
    assert "NX MCP panel" in error.value.suggestion
    engineer.part.Save.assert_not_called()
    # The engineer ticks "Allow agent to change test" on the panel.
    assert dispatch.grant_state(engineer.e) == {"name": "test", "granted": False}
    assert dispatch.set_grant(engineer.e, True) == "test"
    assert dispatch.grant_state(engineer.e) == {"name": "test", "granted": True}
    assert engineer.run("nx_save_part", {})["ok"]
    dispatch.set_grant(engineer.e, False)
    with pytest.raises(NXToolError):
        engineer.run("nx_save_part", {})


def test_reads_still_run_on_the_engineers_part(engineer):
    assert engineer.run("nx_list_objects", {"kind": "body"})["ok"]


@pytest.mark.parametrize(
    ("method", "args"),
    [("nx_save_part", {}), ("nx_save_part", {"path": "copy.prt"}), ("nx_close_part", {})],
)
def test_save_and_close_need_ownership(engineer, method, args, monkeypatch):
    # The parts_files catalogue tools call tx.require_owned before saving or closing.
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)
    engineer.part.Save = Mock()
    engineer.part.SaveAs = Mock()
    engineer.part.Close = Mock()
    with pytest.raises(dispatch.KernelError) as error:
        engineer.run(method, args)
    assert error.value.code == "NX_PART_NOT_GRANTED"
    assert error.value.details["mutation_outcome"] == "not_started"
    engineer.part.Save.assert_not_called()
    engineer.part.SaveAs.assert_not_called()
    engineer.part.Close.assert_not_called()


def test_close_checks_the_named_part_not_the_work_part(engineer, tmp_path, monkeypatch):
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)
    engineer.services.own(engineer.part)
    other = _load(engineer, tmp_path / "theirs.prt")
    other.Close = Mock()
    ref = engineer.e.register(other, "part", other, "Part")["id"]
    with pytest.raises(dispatch.KernelError) as error:
        engineer.run("nx_close_part", {"part": ref, "save": False})
    assert error.value.code == "NX_PART_NOT_GRANTED" and "theirs.prt" in error.value.message
    other.Close.assert_not_called()


def test_development_mode_never_gates(engineer, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    _edit(engineer)
    assert engineer.run("nx_test_edit", {})["status"] == "success"
    assert dispatch.grant_state(engineer.e) is None  # no panel checkbox in development


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

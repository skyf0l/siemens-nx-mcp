"""Who may close, activate or save a part (core/busy.py): the engineer's parts never (save
only when granted, development mode included); another agent's part (caller ids differ)
only with ``force``; with no identity telling callers apart, a close is refused only on a
shared connection discarding unsaved changes a call made and worked in recently. The
caller's own parts never misfire, and refusals name the part being closed."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace as NS

from nx_mcp.core import busy
from nx_mcp.domains.parts_files import parts as tools
from tests.kit.kernel import services
from tests.unit.parts_files.support import call
from tests.unit.parts_files.test_close_multipart import BLOCK, _tools, loaded  # noqa: F401

A = {"id": "sc-a.1", "shared": False}  # agent A's own sidecar
B = {"id": "sc-b.1", "shared": False}  # agent B's own sidecar
SHARED = {"id": "sc-s.1", "shared": True}  # several agents behind one sidecar


def create(nx, path, caller=None, **args):
    made = call(nx, tools.nx_create_part, path=path, **({"caller": caller} if caller else {}),
                **args)  # fmt: skip
    assert made.ok, made
    return made


def block(nx, part, caller=None):
    made = call(nx, NS(spec=BLOCK), part=part, **({"caller": caller} if caller else {}))
    assert made.ok, made
    return made


def close(nx, part, caller=None, **args):
    args.setdefault("save", False)
    return call(nx, tools.nx_close_part, part=part, **({"caller": caller} if caller else {}),
                **args)  # fmt: skip


def engineer_part(nx, name="eng.prt"):
    part = nx.new_part(f"e/{name}")
    part.Save()  # its file exists
    part.IsModified = True  # unsaved engineer work
    services(nx).foreign.add(services(nx).part_key(part))
    return part


# ---------------------------------------------------------------------------
# Engineer parts


def test_the_engineers_part_is_never_closed_even_forced_in_development_mode(nx):
    eng = engineer_part(nx)
    assert services(nx).dev  # development mode: the rule holds there too
    for args in ({}, {"force": True}, {"save": True}):
        refused = close(nx, "eng", A, **args)
        assert refused.error["code"] == "NX_ENGINEER_PART", refused
        assert refused.error["category"] == "blocked" and refused.error["field"] == "/part"
        assert refused.mutation_outcome == "not_started"
        assert refused.error["message"].startswith("eng.prt is the engineer's part")
        assert refused.error["details"] == {"part": "eng.prt", "action": "close"}
    assert eng in nx.session.Parts and eng.IsModified


def test_the_engineers_part_is_never_activated_or_saved_but_reads_reach_it(nx):
    eng = engineer_part(nx)
    for args in ({}, {"force": True}, {"display": True}):
        shown = call(nx, tools.nx_activate_part, part="eng", caller=A, **args)
        assert shown.error["code"] == "NX_ENGINEER_PART", shown
        assert '"part": "eng.prt"' in shown.error["hint"]
    saved = call(nx, tools.nx_save_part, part="eng", caller=A)
    assert saved.error["code"] == "NX_ENGINEER_PART" and "Allow agent" in saved.error["hint"]
    assert eng.IsModified  # not saved
    listed = call(nx, tools.nx_list_parts)
    assert any(r["path"] == "e/eng.prt" and not r["owned"] for r in listed.data["items"])


def test_a_granted_engineer_part_may_be_saved_but_never_closed(nx):
    eng = engineer_part(nx)
    services(nx).granted = lambda part: part is eng
    saved = call(nx, tools.nx_save_part, part="eng", caller=A)
    assert saved.ok and not eng.IsModified, saved
    assert close(nx, "eng", A).error["code"] == "NX_ENGINEER_PART"


def test_opening_the_file_of_a_loaded_engineer_part_leaves_it_alone(nx):
    create(nx, "mc/a.prt", A, display=True)  # the agent's tab is the active one
    eng = engineer_part(nx)
    shown = nx.session.Parts.Display = nx.session.Parts.Work = _part(nx, "a")
    again = call(nx, tools.nx_open_part, path="e/eng.prt", caller=A)
    assert again.ok and again.data["already_loaded"] and not again.data["display"], again
    assert nx.session.Parts.Display is shown and nx.session.Parts.Work is not eng
    assert "already loaded as the engineer's part" in again.warnings[0]["message"]


# ---------------------------------------------------------------------------
# Another agent's part (identity)


def test_another_agents_part_is_refused_by_name_and_force_closes_it(nx):
    create(nx, "mc/a.prt", A)
    create(nx, "mc/b.prt", B, display=False)
    block(nx, "b", B)
    assert call(nx, tools.nx_save_part, part="b", caller=B).ok  # saved: still B's
    refused = close(nx, "b", A)
    assert refused.error["code"] == "NX_PART_BUSY", refused
    assert refused.error["message"] == (
        "b.prt belongs to another agent: another MCP connection (sc-b.1) loaded it"
    )
    assert refused.error["details"] == {
        "part": "b.prt", "reason": "other_agent", "owner": "sc-b.1", "action": "close"
    }  # fmt: skip
    assert "force: true" in refused.error["hint"] and "b" in loaded(nx)
    forced = close(nx, "b", A, force=True)
    assert forced.ok and [r["name"] for r in forced.data["closed"]] == ["b"], forced


def test_another_agents_part_is_activated_only_with_force_and_saving_it_is_allowed(nx):
    create(nx, "mc/a.prt", A)
    create(nx, "mc/b.prt", B, display=False)
    refused = call(nx, tools.nx_activate_part, part="b", caller=A)
    assert refused.error["code"] == "NX_PART_BUSY", refused
    assert refused.error["details"]["action"] == "activate"
    assert call(nx, tools.nx_activate_part, part="b", caller=A, force=True).ok
    assert call(nx, tools.nx_activate_part, part="b", caller=B).ok  # its own
    assert call(nx, tools.nx_save_part, part="b", caller=A).ok  # a save loses no one's work


def test_reopening_another_agents_part_never_shows_it(nx):
    create(nx, "mc/a.prt", A, display=True)
    create(nx, "mc/b.prt", B, display=False)
    assert call(nx, tools.nx_save_part, part="b", caller=B).ok
    shown = nx.session.Parts.Display
    again = call(nx, tools.nx_open_part, path="mc/b.prt", caller=A, display=True)
    assert again.ok and again.data["already_loaded"] and nx.session.Parts.Display is shown
    assert "already loaded as another agent's part" in again.warnings[0]["message"]


def test_own_parts_close_whatever_the_other_agent_did_last(nx):
    """With many agents, an agent's own close must not be refused NX_PART_BUSY."""
    create(nx, "mc/a.prt", A)
    create(nx, "mc/b.prt", B, display=False)
    block(nx, "a", A)
    block(nx, "b", B)  # the other agent's call comes last
    closed = close(nx, "a", A)
    assert closed.ok, closed


# ---------------------------------------------------------------------------
# No identity telling callers apart


def test_a_shared_connection_refuses_discarding_recent_unsaved_work_naming_the_part(nx):
    create(nx, "mc/a.prt", SHARED)
    create(nx, "mc/b.prt", SHARED, display=False)
    block(nx, "a", SHARED)
    block(nx, "b", SHARED)
    refused = close(nx, "a", SHARED)
    assert refused.error["code"] == "NX_PART_BUSY", refused
    message = refused.error["message"]
    assert message.startswith("Closing a.prt would discard its unsaved changes (")
    assert "b.prt" not in message and "Several agents share this MCP connection" in message
    details = refused.error["details"]
    assert details["part"] == "a.prt" and details["reason"] == "recent_change"
    assert details["last_change"]["tool"] == "nx_create_test_block"
    assert details["recent_seconds"] == busy.RECENT_SECONDS
    assert "force: true" in refused.error["hint"] and "save: true" in refused.error["hint"]
    assert close(nx, "a", SHARED, force=True).ok


def test_a_solo_connection_or_no_identity_never_refuses_its_own_close(nx):
    for caller in (A, None):
        create(nx, "mc/a.prt", caller)
        create(nx, "mc/b.prt", caller, display=False)
        block(nx, "a", caller)
        block(nx, "b", caller)
        assert close(nx, "a", caller).ok
        assert close(nx, "b", caller).ok


def test_old_work_or_an_unmodified_part_is_not_busy_on_a_shared_connection(nx):
    create(nx, "mc/a.prt", SHARED)
    create(nx, "mc/b.prt", SHARED, display=False)
    block(nx, "a", SHARED)
    block(nx, "b", SHARED)
    entry = services(nx).part_activity()["parts"][services(nx).part_key(_part(nx, "a"))]
    entry["at"] -= busy.RECENT_SECONDS + 1  # nobody worked in it for a while
    assert close(nx, "a", SHARED).ok
    _part(nx, "b").IsModified = False  # NX says nothing would be lost
    assert close(nx, "b", SHARED).ok


def test_a_part_of_unknown_owner_falls_back_to_the_shared_rule(nx):
    create(nx, "mc/a.prt")  # an older sidecar: no caller key
    block(nx, "a")
    assert busy.owner(services(nx), _part(nx, "a")) is None
    assert close(nx, "a", A).ok  # not refused as another agent's part


def _part(nx, name):
    return next(p for p in nx.session.Parts if p.Name == name)


# ---------------------------------------------------------------------------
# Owners survive a host reload in the same NX process


def test_owners_survive_a_host_reload_in_the_same_nx_process(nx, monkeypatch):
    svc = services(nx)
    monkeypatch.setattr(svc, "created", lambda part: svc.part_key(part) in svc.owned,
                        raising=False)  # fmt: skip
    create(nx, "mc/a.prt", A)
    part = _part(nx, "a")
    assert busy.owner(svc, part) == "sc-a.1"
    path = svc.root / ".nx-mcp" / busy.OWNERS_FILE
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["pid"] == os.getpid()
    assert saved["parts"][str(part.Tag)]["caller"] == "sc-a.1"
    svc.__dict__.pop("_part_activity")  # a reload: new services, nothing known
    assert busy.engineer(svc, part)
    busy.adopt(svc)
    assert not busy.engineer(svc, part) and busy.owner(svc, part) == "sc-a.1"
    svc.owned.clear()  # another NX process: the tags mean nothing
    svc.__dict__.pop("_part_activity")
    path.write_text(json.dumps({**saved, "pid": -1}), encoding="utf-8")
    busy.adopt(svc)
    assert busy.engineer(svc, part)


def test_the_caller_key_is_read_leniently_and_never_reaches_the_handler(nx):
    from nx_mcp.core import target

    assert target.check_caller({"id": " sc-x.1 ", "shared": True}) == {
        "id": "sc-x.1", "shared": True
    }  # fmt: skip
    assert target.check_caller("sc-x.2") == {"id": "sc-x.2", "shared": False}
    for bad in (None, 5, {"id": ""}, {"id": "x" * 300}, {"shared": True}, ["sc"]):
        assert target.check_caller(bad) is None
    made = create(nx, "mc/a.prt", {"id": 7})  # malformed: no identity, not refused
    assert made.ok and busy.owner(services(nx), _part(nx, "a")) is None

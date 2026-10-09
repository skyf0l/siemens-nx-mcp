"""HardenedExecutor.execute: catalogue tools go to the kernel, the rest stays legacy."""

from pathlib import Path

import pytest

from nx_mcp.core import catalogue
from nx_mcp.host import dispatch
from nx_mcp.host.dispatch import KernelError
from tests.kit.objects import Part


def test_catalogue_names_do_not_shadow_legacy_handlers(rig):
    assert not set(catalogue.load()) & set(rig.e._handlers)


def test_catalogue_tool_returns_the_envelope(rig):
    body = rig.nx.Body("B1")
    rig.part.Bodies.append(body)
    ok = rig.e.execute("nx_list_objects", {"kind": "body", "intent": "look", "detail": "full"})
    assert ok["ok"] is True and ok["tool"] == "nx_list_objects"
    assert ok["mutation_outcome"] == "not_applicable" and ok["page"]["total"] == 1
    row = ok["data"]["items"][0]
    assert row["id"].startswith("b:") and row["part"].startswith("p:") and row["kind"] == "body"
    assert row["anchor"].startswith("@test.prt:")
    # The short ref and the legacy obj_ spelling resolve to the same body.
    legacy = rig.ref(body)
    by_legacy = rig.e.execute("nx_list_objects", {"kind": "body", "owner": legacy})
    by_short = rig.e.execute("nx_list_objects", {"kind": "body", "owner": row["id"]})
    assert by_legacy["data"]["items"][0]["id"] == by_short["data"]["items"][0]["id"] == row["id"]


def test_catalogue_failure_raises_kernel_error_with_envelope(rig):
    with pytest.raises(KernelError) as error:
        rig.e.execute("nx_list_objects", {"kind": "widget", "operation_id": "op_abcdefgh"})
    payload = error.value.as_dict()
    assert payload["status"] == "error" and payload["code"] == "NX_INVALID_ARGUMENT"
    envelope = payload["details"]["envelope"]
    assert envelope["ok"] is False and envelope["error"]["field"] == "/kind"
    assert envelope["operation_id"] == "op_abcdefgh"
    assert payload["details"]["mutation_outcome"] == "not_applicable"


def test_untested_model_tool_is_refused_in_production(rig, monkeypatch):
    monkeypatch.delenv("NX_MCP_DEV", raising=False)
    with pytest.raises(KernelError) as error:
        rig.e.execute(
            "nx_create_datum_plane",
            {"method": "point_normal", "origin": [0, 0, 0], "normal": [0, 0, 1]},
        )
    assert error.value.code == "NX_VERSION_UNSUPPORTED"
    assert error.value.details["mutation_outcome"] == "not_started"
    assert rig.session.marks == {}


def test_model_tool_in_dev_uses_one_visible_mark_and_legacy_history(rig, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    with pytest.raises(KernelError) as error:
        rig.e.execute(
            "nx_create_datum_plane",
            {"method": "three_points", "points": [[0, 0, 0], [1, 1, 1], [2, 2, 2]]},
        )
    assert error.value.details["envelope"]["error"]["field"] == "/points"
    assert error.value.details["mutation_outcome"] == "not_started"
    assert rig.session.marks == {} and rig.e._history == []


def test_legacy_tools_keep_their_shape_and_record_ownership(rig, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")  # tracking runs in development too
    legacy = rig.e.execute("nx_list_bodies", {})
    assert legacy["status"] == "success" and "ok" not in legacy
    services = dispatch.services_for(rig.e)
    rig.e._owned_parts.clear()
    # The engineer's part was loaded before any agent call: not owned.
    assert not services.owns(rig.part)
    # A part loaded by an agent call (created or opened) is owned.
    rig.e._handlers["nx_test_open"] = lambda: (
        bool(Part(rig.session, Path(rig.part.FullPath).parent / "new.prt")) and {"opened": 1}
    )
    rig.e.execute("nx_test_open", {})
    new = rig.session.Parts[-1]
    assert services.owns(new) and services.created(new) and not services.owns(rig.part)


def test_panel_summary_comes_from_the_spec(rig):
    line = dispatch.render_summary(
        rig.e, "nx_create_datum_plane", {"method": "offset", "reference": "fc:7", "distance": 15}
    )
    assert line == "Datum plane 15 mm from fc:7"
    assert dispatch.render_summary(rig.e, "nx_list_bodies", {}) is None
    assert dispatch.is_read_only("nx_list_objects") is True
    assert dispatch.is_read_only("nx_create_datum_plane") is False
    assert dispatch.is_read_only("nx_list_bodies") is None

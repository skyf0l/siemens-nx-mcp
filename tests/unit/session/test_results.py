"""nx_get_result and nx_delete_results (domains/session/results.py)."""

import os
import time
from unittest.mock import AsyncMock

from nx_mcp.agent_surface import ResultStore
from nx_mcp.domains.session import results as rs
from nx_mcp.result_transport import bound_result
from nx_mcp.sidecar.proxy import CatalogueProxy
from nx_mcp.workspace import Workspace
from tests.unit.parts_files.support import side

ROW = {"id": "c:1", "kind": "curve", "name": "LINE", "part": "p:1"}


def stored(root, payload) -> str:
    return ResultStore(root).put(payload)


def envelope(n=45):
    rows = [{**ROW, "id": f"c:{i}", "points": list(range(25))} for i in range(n)]
    return {"ok": True, "tool": "nx_get_sketch", "data": {"sketch": ROW, "curves": rows}}


def test_lists_page_with_compact_rows_and_pointers(tmp_path):
    key = stored(tmp_path, envelope())
    ok = side(tmp_path, rs.nx_get_result, result_id=key, field="/data/curves", offset=40)
    assert ok.ok and ok.mutation_outcome == "not_applicable"
    assert ok.data["kind"] == "list" and [r["id"] for r in ok.data["items"]] == [
        f"c:{i}" for i in range(40, 45)
    ]
    assert ok.page == {"offset": 40, "limit": 20, "returned": 5, "total": 45, "next_offset": None}
    assert len(ok.data["items"][0]["points"]) == 20  # compact cuts nested arrays
    assert ok.data["omitted"]["/data/curves/40/points"] == {"total": 25, "returned": 20}
    full = side(tmp_path, rs.nx_get_result, result_id=key, field="/data/curves", detail="full")
    assert len(full.data["items"][0]["points"]) == 25 and "omitted" not in full.data
    assert full.page["next_offset"] == 20


def test_objects_page_keys_and_pointers_reach_scalars(tmp_path):
    key = stored(tmp_path, envelope())
    root = side(tmp_path, rs.nx_get_result, result_id=key)
    assert root.data["kind"] == "object" and set(root.data["value"]) == {"ok", "tool", "data"}
    assert root.page["total"] == 3
    scalar = side(tmp_path, rs.nx_get_result, result_id=key, field="/data/curves/7/id")
    assert scalar.data["kind"] == "text" and scalar.data["value"] == "c:7"
    flag = side(tmp_path, rs.nx_get_result, result_id=key, field="/ok")
    assert flag.data == {"result_id": key, "field": "/ok", "kind": "value", "value": True}
    ref = side(tmp_path, rs.nx_get_result, result_id=key, field="/data/sketch")
    assert ref.data["value"] == ROW  # references stay whole


def test_escaped_pointers_and_text_pages(tmp_path):
    key = stored(tmp_path, {"a/b": {"~x": "y" * 2500}})
    text = side(tmp_path, rs.nx_get_result, result_id=key, field="/a~1b/~0x", limit=1)
    assert text.data["kind"] == "text" and text.data["value"] == "y" * 1000
    assert text.page["total"] == 2500 and text.page["next_offset"] == 1000
    compact = side(tmp_path, rs.nx_get_result, result_id=key, field="/a~1b")
    assert len(compact.data["value"]["~x"]) == 2048
    assert compact.data["omitted"] == {"/a~1b/~0x": {"total": 2500, "returned": 2048}}


def test_oversized_pages_name_each_entry(tmp_path):
    key = stored(tmp_path, {"rows": [["x" * 2000] * 20 for _ in range(20)]})
    ok = side(tmp_path, rs.nx_get_result, result_id=key, field="/rows", detail="full")
    assert ok.data["items"][3] == {"field": "/rows/3", "read_separately": True}
    assert "/rows" in ok.data["omitted"]


def test_bridge_snapshots_share_the_tool(tmp_path):
    full = {"status": "success", "objects": ["x" * 2048] * 300, "mutation_outcome": "committed"}
    receipt = bound_result(full, tmp_path / ".nx-mcp" / "bridge-results")
    assert receipt["full_result"]["tool"] == "nx_get_result"
    ok = side(tmp_path, rs.nx_get_result, result_id=receipt["full_result"]["id"],
              field="/objects", offset=290)  # fmt: skip
    assert ok.page["returned"] == 10 and ok.page["total"] == 300


def test_unknown_ids_bad_pointers_and_limits_are_refused(tmp_path):
    key = stored(tmp_path, envelope())
    gone = side(tmp_path, rs.nx_get_result, result_id="result_" + "0" * 32)
    assert gone.error["code"] == "NX_RESULT_EXPIRED" and gone.error["field"] == "/result_id"
    assert gone.mutation_outcome == "not_applicable"
    for args, field in [
        ({"field": "/data/nope"}, "/field"),
        ({"field": "/data/curves/45"}, "/field"),
        ({"field": "/data/curves/x"}, "/field"),
        ({"field": "data"}, "/field"),
        ({"limit": 101}, "/limit"),
        ({"offset": -1}, "/offset"),
    ]:
        bad = side(tmp_path, rs.nx_get_result, result_id=key, **args)
        assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == field, args
    traversal = side(tmp_path, rs.nx_get_result, result_id="../operations/x")
    assert traversal.error["code"] == "NX_INVALID_ARGUMENT"


def test_delete_previews_then_prunes_both_stores(tmp_path):
    old = stored(tmp_path, {"old": 1})
    path = tmp_path / ".nx-mcp" / "agent-results" / (old + ".json")
    os.utime(path, (time.time() - 100, time.time() - 100))
    new = stored(tmp_path, {"new": 1})
    bridge = bound_result({"objects": ["x" * 2048] * 300}, tmp_path / ".nx-mcp" / "bridge-results")
    receipt = tmp_path / ".nx-mcp" / "operations" / "op.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("keep")
    preview = side(tmp_path, rs.nx_delete_results, max_age_seconds=50)
    assert preview.ok
    assert preview.data["applied"] is False and preview.data["snapshot_count"] == 3
    assert preview.data["selected_count"] == 1 and path.exists()
    done = side(tmp_path, rs.nx_delete_results, max_age_seconds=50, dry_run=False)
    assert done.data["applied"] and done.data["selected_count"] == 1 and not path.exists()
    assert side(tmp_path, rs.nx_get_result, result_id=new).ok
    everything = side(tmp_path, rs.nx_delete_results, max_bytes=1, dry_run=False)
    assert everything.data["selected_count"] == 2 and everything.data["bytes_after_cleanup"] == 0
    gone = side(tmp_path, rs.nx_get_result, result_id=bridge["full_result"]["id"])
    assert gone.error["code"] == "NX_RESULT_EXPIRED"
    assert receipt.read_text() == "keep"


def test_delete_exact_ids(tmp_path):
    a, b = stored(tmp_path, {"a": 1}), stored(tmp_path, {"b": 2})
    missing = "result_" + "f" * 32
    preview = side(tmp_path, rs.nx_delete_results, result_ids=[a, missing])
    assert preview.data["selected_count"] == 1 and preview.data["missing"] == [missing]
    assert side(tmp_path, rs.nx_get_result, result_id=a).ok
    done = side(tmp_path, rs.nx_delete_results, result_ids=[a], dry_run=False)
    assert done.ok and done.data["applied"] and done.data["snapshot_count"] == 2
    assert side(tmp_path, rs.nx_get_result, result_id=a).error["code"] == "NX_RESULT_EXPIRED"
    assert side(tmp_path, rs.nx_get_result, result_id=b).ok
    bad = side(tmp_path, rs.nx_delete_results, max_bytes=0)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT"


async def test_proxy_forwards_detail_and_never_calls_nx(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    key = stored(tmp_path, envelope())
    bridge = AsyncMock()
    proxy = CatalogueProxy(bridge, Workspace(tmp_path), ResultStore(tmp_path), build="v2406")
    spec = rs.nx_get_result.spec
    args = {"result_id": key, "field": "/data/curves", "limit": 1}
    compact = (await proxy.call(spec, dict(args))).structuredContent
    assert len(compact["data"]["items"][0]["points"]) == 20 and "result_id" not in compact
    full = (await proxy.call(spec, {**args, "detail": "full"})).structuredContent
    assert len(full["data"]["items"][0]["points"]) == 25
    assert not bridge.call.called

"""Safety and MCP contract regressions; these do not substitute for real NX tests."""

import asyncio
from threading import Event
from types import SimpleNamespace

import pytest

from nx_mcp.bridge import BridgeClient, BridgeServer
from nx_mcp.hardened import HardenedExecutor
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace


class FakeSession:
    def __init__(self):
        self.Parts = SimpleNamespace(Work=None)
        self.values = []
        self.marks = {}
        self.next_mark = 0

    def SetUndoMark(self, *args):
        self.next_mark += 1
        self.marks[self.next_mark] = list(self.values)
        return self.next_mark

    def UndoToMark(self, mark, *args):
        self.values[:] = self.marks[mark]

    def DeleteUndoMark(self, mark, *args):
        self.marks.pop(mark, None)

    def DoesUndoMarkExist(self, mark, *args):
        return mark in self.marks


@pytest.fixture
def executor(tmp_path):
    session = FakeSession()
    e = HardenedExecutor(
        session,
        SimpleNamespace(Session=SimpleNamespace(MarkVisibility=SimpleNamespace(Visible=1))),
        "test",
        Workspace(tmp_path),
    )

    def mutate(value=1, fail=False):
        session.values.append(value)
        if fail:
            raise NXToolError("TEST_FAIL", "failure after a model change")
        return {"values": list(session.values)}

    e._handlers["nx_test_mutate"] = mutate
    return e


def test_retry_committed_mutation_is_deduplicated(executor):
    p = {"value": 1, "operation_id": "same-request-123"}
    a = executor.execute("nx_test_mutate", p)
    b = executor.execute("nx_test_mutate", p)
    assert executor.session.values == [1]
    assert b["replayed"] and a["operation_id"] == b["operation_id"]
    with pytest.raises(NXToolError, match="different arguments"):
        executor.execute("nx_test_mutate", dict(p, value=2))


def test_failed_partial_mutation_rolls_back_and_cannot_reapply(executor):
    p = {"value": 4, "fail": True, "operation_id": "failed-request-123"}
    with pytest.raises(NXToolError) as error:
        executor.execute("nx_test_mutate", p)
    assert error.value.details["mutation_outcome"] == "rolled_back"
    assert executor.session.values == []
    assert executor.store.get(p["operation_id"])["state"] == "failed"
    with pytest.raises(NXToolError):
        executor.execute("nx_test_mutate", p)
    assert executor.session.values == []


def test_restarted_pending_receipt_becomes_unknown(executor):
    store = executor.store
    store.put(
        {
            "operation_id": "crashed-request-123",
            "session_id": "old",
            "state": "running",
            "fingerprint": "a",
        }
    )
    store.recover(executor.session_id)
    assert store.get("crashed-request-123")["state"] == "unknown"
    assert store.get("never-seen-request")["mutation_outcome"] == "unknown"


@pytest.mark.asyncio
async def test_transport_timeout_does_not_duplicate_mutation(executor):
    handler = executor._handlers["nx_test_mutate"]
    entered, release = Event(), Event()

    def delayed(**params):
        entered.set()
        assert release.wait(15), "Test did not release the received mutation"
        return handler(**params)

    executor._handlers["nx_test_mutate"] = delayed
    server = BridgeServer(executor.execute, token="test-token")
    server.start()
    p = {"value": 9, "operation_id": "lost-response-123"}
    first = asyncio.create_task(
        BridgeClient("127.0.0.1", server.port, token="test-token", timeout=10).call(
            "nx_test_mutate", p
        )
    )
    try:
        # Start the response deadline only after the request reached the server.
        # A 20ms connect deadline can expire before receipt on Windows CI.
        assert await asyncio.to_thread(entered.wait, 5), "Server did not receive first request"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(first, timeout=0.02)
        release.set()
        result = await BridgeClient("127.0.0.1", server.port, token="test-token", timeout=10).call(
            "nx_test_mutate", p
        )
        assert result["replayed"] and executor.session.values == [9]
    finally:
        release.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        server.stop()


@pytest.mark.asyncio
async def test_uniform_structured_success_failure_and_schema(tmp_path):
    class Caller:
        async def call(self, method, params):
            if method == "nx_list_topology":
                raise NXToolError("NX_UNSUPPORTED_EDIT", "unsupported edit")
            return {"status": "success", "method": method, "params": params}

    server = create_server(Caller(), Workspace(tmp_path), surface="full")
    tools = await server.list_tools()
    move = next(t for t in tools if t.name == "nx_rename_object")
    assert "name" in move.inputSchema["required"]
    assert "operation_id" in move.inputSchema["properties"]
    success = await server.call_tool("nx_status", {})
    assert success.structuredContent["status"] == "success" and not success.isError
    for name, args in [
        ("nx_list_topology", {"body": "a"}),
        ("nx_rename_object", {"object_id": "c", "ignored": 123}),
    ]:
        result = await server.call_tool(name, args)
        assert result.isError and result.structuredContent["status"] == "error"
    assert not next(t for t in tools if t.name == "nx_rename_object").annotations.readOnlyHint


def test_reference_namespace_rejects_previous_session(executor, tmp_path):
    obj = SimpleNamespace(Tag=123, Name="Body")
    first = executor.objects.register(obj, kind="body", name="Body", part_id="part_test")
    assert first.id.startswith("obj_" + executor.session_id + "_")
    another = HardenedExecutor(
        FakeSession(),
        SimpleNamespace(Session=SimpleNamespace(MarkVisibility=SimpleNamespace(Visible=1))),
        "test",
        Workspace(tmp_path / "other"),
    )
    with pytest.raises(NXToolError) as error:
        another.objects.resolve(first.id)
    assert error.value.code == "NX_OBJECT_STALE"


def test_new_feature_inheriting_a_rolled_back_error_is_updated_once(executor):
    # NX 2406 attaches a rolled-back feature's error to the next feature created
    # under the same name; one update of that new feature drops the stale message.
    class Feature:
        def __init__(self, tag, errors):
            self.Tag, self.errors = tag, errors

        def GetFeatureErrorMessages(self):
            return self.errors

    old, clean, inherited = Feature(1, ["old error"]), Feature(2, []), Feature(3, ["stale"])
    logged = []

    def do_update(mark):
        for feature in logged:
            feature.errors = []
        return 0

    executor.session.UpdateManager = SimpleNamespace(LogForUpdate=logged.append, DoUpdate=do_update)
    executor._active_mark = 7
    part = SimpleNamespace(Features=[old, clean, inherited])
    executor._drop_inherited_feature_errors(part, {("feature", 1): {}})
    assert logged == [inherited]
    assert old.errors == ["old error"] and inherited.errors == []


def test_tool_disabled_for_this_nx_version_is_refused_before_nx(executor):
    executor.nx_version = "v2406"
    executor._manifest_tools = {"nx_test_mutate": {"unavailable_on": {"v2406": "API missing"}}}
    with pytest.raises(NXToolError) as error:
        executor.execute("nx_test_mutate", {"value": 1})
    assert error.value.code == "NX_UNAVAILABLE_ON_VERSION"
    assert "API missing" in error.value.message
    assert executor.session.values == [] and not executor.session.marks
    executor.nx_version = "v2606"
    assert executor.execute("nx_test_mutate", {"value": 1})["values"] == [1]


def test_capabilities_report_version_disabled_tools_as_unavailable():
    from nx_mcp.manifest import scope_to_version

    manifest = {
        "tools": {
            "a": {"status": "tested", "verified_versions": ["v2406"], "unavailable_on": {}},
            "b": {
                "status": "tested",
                "verified_versions": ["v2606"],
                "unavailable_on": {"v2406": "x"},
            },
        }
    }
    tools = scope_to_version(manifest, "v2406")["tools"]
    assert tools["a"]["status"] == "tested"
    assert tools["b"]["status"] == "unavailable" and "x" in tools["b"]["scope"]

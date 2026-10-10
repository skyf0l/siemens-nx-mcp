"""Acceptance runner: expect operators, resolution, a scenario run on a fake bridge, fold."""

import json
import re
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.acceptance import expect, runner
from nx_mcp.core import catalogue
from nx_mcp.core.errors import NXError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace

RESULT = {
    "ok": True,
    "mutation_outcome": "committed",
    "data": {"origin": [0, 0, 25.0000001], "normal": [0, 0, 1], "name": "MID_PLANE", "on": True},
    "changes": {"created": [{"kind": "feature"}, {"kind": "datum_plane"}]},
    "page": {"total": 5},
}


@pytest.mark.parametrize(
    "path, rule",
    [
        ("mutation_outcome", "committed"),
        ("data.origin.2", {"close": 25, "tol": 1e-6}),
        ("data.origin.-1", {"approx": 25, "tol": 1e-6}),
        ("data.normal", {"close": [0, 0, 1], "tol": 1e-9}),
        ("changes.created", {"count": 2}),
        ("changes.created", {"count_kind": {"feature": 1, "datum_plane": 1}}),
        ("changes.created", {"contains": {"kind": "feature"}}),
        ("data.name", {"matches": "(?i)^mid_plane$"}),
        ("data.name", {"contains": "PLANE"}),
        ("data.on", True),
        ("page.total", {"equals": 5}),
    ],
)
def test_operators_pass(path, rule):
    assert expect.check(RESULT, path, rule) is None


@pytest.mark.parametrize(
    "path, rule",
    [
        ("mutation_outcome", "rolled_back"),
        ("data.origin.2", {"close": 26, "tol": 1e-6}),
        ("data.normal", {"close": [0, 1], "tol": 1}),
        ("changes.created", {"count": 1}),
        ("changes.created", {"count_kind": {"feature": 2}}),
        ("data.name", {"matches": "^x"}),
        ("data.on", 1),  # true never equals 1
        ("error.code", "NX_ERROR"),  # missing path
        ("data.origin.7", 0),
    ],
)
def test_operators_fail_with_a_message(path, rule):
    assert path.split(".")[0] in expect.check(RESULT, path, rule)


def test_resolve_specials_and_saved_pointers():
    saved = {"top": {"items": [{"object": {"id": "obj_1"}}]}}
    special = {"$part": "C:/w/smoke/r/a.prt"}
    args = {"path": "$part", "reference": "$top.items.0.object.id", "keep": ["$top.items.0"]}
    assert runner.resolve(args, saved, special) == {
        "path": "C:/w/smoke/r/a.prt",
        "reference": "obj_1",
        "keep": [{"object": {"id": "obj_1"}}],
    }
    for bad in ("$nope.x", "$top.items.3"):
        with pytest.raises(KeyError):
            runner.resolve(bad, saved, special)


def test_double_dollar_passes_a_literal_batch_reference():
    args = {"operations": [{"arguments": {"sketch": "$$0.data.sketch", "name": "$$part"}}]}
    assert runner.resolve(args, {}, {"$part": "C:/w/a.prt"}) == {
        "operations": [{"arguments": {"sketch": "$0.data.sketch", "name": "$part"}}]
    }


def test_unwrap_gives_every_result_the_envelope_shape():
    assert runner.unwrap({"status": "success", "x": 1}) == {"status": "success", "x": 1, "ok": True}
    assert runner.unwrap({"ok": False, "error": {}}) == {"ok": False, "error": {}}
    assert runner.unwrap("text")["error"]["code"] == "NX_ERROR"


def test_scenario_format_is_checked(tmp_path):
    bad = tmp_path / "bad.json"
    for content in [
        {"steps": []},
        {"covers": {"t": []}, "steps": [{"args": {}}]},
        {"covers": {"t": []}, "steps": [{"call": "x", "expected": {}}]},
        {"covers": {"t": []}, "steps": [{"call": "x", "expect": {"a": {"close": 1, "bogus": 2}}}]},
        {"covers": {"t": []}, "cleanup": "forget", "steps": [{"call": "x"}]},
    ]:
        bad.write_text(json.dumps(content), encoding="utf-8")
        with pytest.raises(runner.ScenarioError):
            runner.load_scenario(bad)


def test_commit_sha_reads_git_files_without_git(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    assert runner.commit_sha(tmp_path) is None
    (git / "packed-refs").write_text("# pack\nabc123 refs/heads/main\n")
    assert runner.commit_sha(tmp_path) == "abc123"
    (git / "refs" / "heads" / "main").write_text("def456\n")
    assert runner.commit_sha(tmp_path) == "def456"


def test_dev_lock_is_held_and_released(tmp_path):
    lock = runner.DevLock(tmp_path, "tester", wait=0)
    lock.acquire()
    assert json.loads(lock.path.read_text())["owner"] == "tester"
    other = runner.DevLock(tmp_path, "other", wait=0)
    with pytest.raises(TimeoutError):
        other.acquire()
    other.release()  # not the owner: keeps the lock
    assert lock.path.exists()
    lock.release()
    assert not lock.path.exists()


def test_dev_lock_already_held_by_the_owner_is_kept(tmp_path):
    held = runner.DevLock(tmp_path, "agent", wait=0)
    held.acquire()  # the agent's own `dev_lock.py acquire`
    run = runner.DevLock(tmp_path, "agent", wait=0)
    run.acquire()
    run.refresh()
    run.release()  # the runner did not acquire it: the agent keeps it
    assert json.loads(run.path.read_text())["owner"] == "agent"
    held.release()
    assert not held.path.exists()


class FakeNX:
    """Answers like the bridge: plain dicts for setup tools, kernel envelopes otherwise."""

    def __init__(self):
        self.calls = []
        # The engineer's part is loaded and displayed before the scenario starts.
        self.parts = [self.row("C:/eng/bracket.prt", display=True)]

    @staticmethod
    def row(path, display=False):
        part = {"id": "obj_" + re.sub(r"[^A-Za-z0-9_]", "_", path)}
        return {"path": path, "part": part, "work": display, "display": display}

    @staticmethod
    def ok(method, data, **extra):
        return {"ok": True, "tool": method, "mutation_outcome": "committed", "data": data, **extra}

    async def call(self, method, params):
        if method not in ("nx_list_parts",):
            self.calls.append((method, params))
        if method == "nx_list_parts":
            items = [dict(p) for p in self.parts]
            page = {"offset": 0, "limit": 200, "returned": len(items), "total": len(items),
                    "next_offset": None}  # fmt: skip
            return self.ok(method, {"items": items}, page=page)
        if method == "nx_close_part":
            assert params["save"] is False
            self.parts = [p for p in self.parts if p["part"]["id"] != params["part"]]
            return self.ok(method, {"closed": [], "closed_count": 1})
        if method == "nx_activate_part":
            for p in self.parts:
                p["work"] = p["display"] = p["part"]["id"] == params["part"]
            return self.ok(method, {})
        if method == "nx_create_part":
            for p in self.parts:
                p["work"] = p["display"] = False
            self.parts.append(self.row(params["path"], display=True))
            return self.ok(method, {"part": {"id": "p:1", "kind": "part"}})
        if method == "nx_find_geometry":
            return {
                "ok": True,
                "tool": method,
                "mutation_outcome": "not_applicable",
                "data": {"items": [{"id": "obj_face_0001", "kind": "face"}], "selector": None},
                "page": {"offset": 0, "limit": 20, "returned": 1, "total": 1, "next_offset": None},
            }
        if method == "nx_list_objects":
            return {
                "ok": True,
                "tool": method,
                "mutation_outcome": "not_applicable",
                "data": {"kind": params["kind"], "items": [{"id": "b:1", "kind": "body"}]},
                "page": {"offset": 0, "limit": 20, "returned": 1, "total": 1, "next_offset": None},
            }
        if params.get("reference") == "obj_face_0001":
            return {
                "ok": True,
                "tool": method,
                "operation_id": params["operation_id"],
                "mutation_outcome": "committed",
                "data": {"origin": [0, 0, 25], "normal": [0, 0, 1]},
                "changes": {"created": [{"kind": "feature"}], "references": "preserved"},
            }
        raise NXError(
            "NX_WRONG_KIND",
            "Expected a face",
            details={
                "mutation_outcome": "not_started",
                "envelope": {
                    "ok": False,
                    "tool": method,
                    "mutation_outcome": "not_started",
                    "error": {"code": "NX_WRONG_KIND", "category": "wrong_kind"},
                },
            },
        )


SCENARIO = {
    "covers": {"nx_create_datum_plane": ["offset"], "nx_list_objects": []},
    "part": "new",
    "steps": [
        {"call": "nx_create_part", "setup": True, "args": {"path": "$part", "units": "mm"}},
        {"call": "nx_find_geometry", "setup": True, "save": "top", "args": {"kind": "face"}},
        {
            "call": "nx_create_datum_plane",
            "save": "dp",
            "args": {"method": "offset", "reference": "$top.data.items.0.id", "distance": 15},
            "expect": {
                "data.origin.2": {"close": 25, "tol": 1e-9},
                "mutation_outcome": "committed",
            },
        },
        {
            "call": "nx_create_datum_plane",
            "args": {"method": "offset", "reference": "b:4", "distance": 5},
            "expect": {"error.code": "NX_WRONG_KIND", "mutation_outcome": "not_started"},
        },
        {
            "call": "nx_create_datum_plane",
            "args": {"method": "offset", "reference": "b:4", "distance": "5 deg"},
            "expect": {"error.field": "/distance", "mutation_outcome": "not_started"},
        },
        {"call": "nx_list_objects", "args": {"kind": "body"}, "expect": {"page.total": 1}},
    ],
}


@pytest.fixture
def scenario_dir(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "acceptance" / "modeling").mkdir(parents=True)
    path = root / "acceptance" / "modeling" / "fake.json"
    path.write_text(json.dumps(SCENARIO), encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "SCENARIOS", root / "acceptance")
    monkeypatch.setenv("NX_MCP_DEV", "1")
    # Never click a real development panel from the unit suite.
    monkeypatch.setattr(runner, "find_dev_panel", lambda: None)
    return path


async def test_scenario_runs_through_the_gateways_and_writes_evidence(tmp_path, scenario_dir):
    nx = FakeNX()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    server = create_server(nx, Workspace(workspace), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        record = await runner.run_scenario(
            session,
            scenario_dir,
            workspace=workspace,
            run_id="r1",
            build=("v2406", "NX 2406.1700"),
            owner="tester",
        )
    assert record["passed"], record["steps"]
    assert record["checks"] == 7 and record["nx_build"] == "NX 2406.1700"
    assert record["scenario"] == "acceptance/modeling/fake.json"
    assert record["sources"]["nx_create_datum_plane"]
    part = nx.calls[0][1]["path"]
    assert part == str(workspace / "smoke" / "r1" / "modeling_fake_r1.prt")
    method, params = nx.calls[2]
    assert method == "nx_create_datum_plane" and params["reference"] == "obj_face_0001"
    assert params["intent"].startswith("Acceptance modeling/fake, step 3")
    assert [c[0] for c in nx.calls].count("nx_create_datum_plane") == 2  # step 5 stays local
    assert all(s["result_sha256"] for s in record["steps"])
    # Cleanup: the scenario part is closed unsaved by ID. The engineer's part is never
    # displayed again by the run (re-displaying a part can switch NX's application).
    assert record["cleanup"]["closed"] == [part] and not record["cleanup"]["failed"]
    assert record["cleanup"]["not_restored"] == "C:/eng/bracket.prt"
    method, params = nx.calls[-1]
    assert method == "nx_close_part" and params["save"] is False
    assert params["part"] == nx.row(part)["part"]["id"]
    assert "nx_activate_part" not in [c[0] for c in nx.calls]
    assert [p["path"] for p in nx.parts] == ["C:/eng/bracket.prt"]

    evidence_root = tmp_path / "evidence"
    written = runner.write_evidence(record, evidence_root)
    assert written == evidence_root / "v2406" / "modeling" / "fake.json"
    folded, problems = runner.fold(evidence_root, tmp_path / "out")
    assert problems == []
    entry = folded["nx_create_datum_plane"]["v2406"]
    assert entry["variants"] == ["offset"] and entry["checks"] == 7
    assert entry["scenario_sha256"] == runner.sha256(scenario_dir.read_bytes())
    assert json.loads((tmp_path / "out" / "inspection.json").read_text())["nx_list_objects"]

    # A changed scenario makes its evidence stale; a failed run never counts.
    scenario_dir.write_text(json.dumps({**SCENARIO, "part": "new "}), encoding="utf-8")
    folded, problems = runner.fold(evidence_root, tmp_path / "out")
    assert folded == {} and "rerun" in problems[0]
    assert not list((tmp_path / "out").glob("*.json"))
    failed = runner.write_evidence({**record, "passed": False}, evidence_root)
    assert failed.name == "fake.last-failure.json"


async def test_a_failing_expectation_fails_the_scenario(tmp_path, scenario_dir):
    broken = json.loads(json.dumps(SCENARIO))
    broken["steps"][2]["expect"]["data.origin.2"] = {"close": 30, "tol": 1e-9}
    scenario_dir.write_text(json.dumps(broken), encoding="utf-8")
    server = create_server(FakeNX(), Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        record = await runner.run_scenario(
            session, scenario_dir, workspace=tmp_path, run_id="r2", build=("v2406", ""), owner="t"
        )
    assert not record["passed"] and record["failed_checks"] == 1
    assert "close 30" in record["steps"][2]["failures"][0]


async def test_parts_are_closed_when_a_setup_step_fails(tmp_path, scenario_dir):
    broken = json.loads(json.dumps(SCENARIO))
    broken["steps"].insert(1, {"call": "nx_create_datum_plane", "args": {"method": "x"}})
    scenario_dir.write_text(json.dumps(broken), encoding="utf-8")
    nx = FakeNX()
    server = create_server(nx, Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        record = await runner.run_scenario(
            session, scenario_dir, workspace=tmp_path, run_id="r3", build=("v2406", ""), owner="t"
        )
    assert not record["passed"] and len(record["steps"]) == 2  # stopped at the failed setup
    assert len(record["cleanup"]["closed"]) == 1
    assert [p["path"] for p in nx.parts] == ["C:/eng/bracket.prt"]


async def test_cleanup_keep_leaves_parts_loaded(tmp_path, scenario_dir):
    scenario_dir.write_text(json.dumps({**SCENARIO, "cleanup": "keep"}), encoding="utf-8")
    nx = FakeNX()
    server = create_server(nx, Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        record = await runner.run_scenario(
            session, scenario_dir, workspace=tmp_path, run_id="r4", build=("v2406", ""), owner="t"
        )
    assert record["cleanup"] == {"mode": "keep", "closed": [], "failed": []}
    assert len(nx.parts) == 2


class PanelNX(FakeNX):
    """A fake interactive host: the agent may lower the panel mode, never raise it."""

    RANK = {"ask": 0, "auto": 1, "agent": 2}

    def __init__(self, mode="agent"):
        super().__init__()
        self.mode = mode
        self.clicks = []

    async def call(self, method, params):
        if method != "nx_set_control":
            return await super().call(method, params)
        self.calls.append((method, params))
        wanted = {"manual": "ask"}.get(params.get("mode"), params.get("mode"))
        if wanted not in (None, "status"):
            if self.RANK[wanted] > self.RANK[self.mode]:
                raise NXError(
                    "NX_CONTROL_PAUSED",
                    "Only the engineer can",
                    details={"mutation_outcome": "not_started"},
                )
            self.mode = wanted
        data = {"mode": self.mode, "mode_label": self.mode, "owner": "agent", "busy": False,
                "waiting": 0, "activity": "idle", "running": None}  # fmt: skip
        return self.ok(method, data)

    def click(self, panel, mode):
        assert panel == 4242
        self.clicks.append(mode)
        self.mode = mode  # the engineer's click: any mode


CONTROL = {
    "covers": {"nx_set_control": ["ask"]},
    "part": None,
    "steps": [{"call": "nx_set_control", "args": {"mode": "ask"}, "expect": {"data.mode": "ask"}}],
}


@pytest.mark.parametrize("dev", [True, False])
async def test_the_dev_panel_mode_is_restored_after_a_scenario(
    tmp_path, scenario_dir, monkeypatch, dev
):
    scenario_dir.write_text(json.dumps(CONTROL), encoding="utf-8")
    nx = PanelNX("agent")
    monkeypatch.setattr(runner, "find_dev_panel", lambda: 4242 if dev else None)
    monkeypatch.setattr(runner, "press_panel", nx.click)
    server = create_server(nx, Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        record = await runner.run_scenario(
            session, scenario_dir, workspace=tmp_path, run_id="r5", build=("v2406", ""), owner="t"
        )
    assert record["passed"], record["steps"]
    if dev:  # "Ask me first" left by the scenario: the runner clicks Agent drives again.
        assert nx.mode == "agent" and nx.clicks == ["agent"]
        assert record["cleanup"]["panel"] == {"from": "ask", "to": "agent", "restored": True}
        assert not record["cleanup"]["failed"]
    else:  # Production panels are never touched, not even read.
        assert nx.mode == "ask" and nx.clicks == [] and "panel" not in record["cleanup"]
        assert [c for c in nx.calls if c[1].get("mode") == "status"] == []


class EngineerNX(PanelNX):
    """The engineer clicks ``engineer`` on the panel during the first scenario step."""

    def __init__(self, mode, engineer):
        super().__init__(mode)
        self.engineer = engineer

    async def call(self, method, params):
        result = await super().call(method, params)
        if self.engineer and method != "nx_list_parts" and params.get("mode") != "status":
            self.mode, self.engineer = self.engineer, None
        return result


@pytest.mark.parametrize(
    ("scenario", "start", "engineer"),
    [(None, "agent", "auto"), (None, "auto", "agent"), (CONTROL, "auto", "agent")],
)
async def test_the_runner_never_overrides_the_engineers_panel_choice(
    tmp_path, scenario_dir, monkeypatch, scenario, start, engineer
):
    """Only a mode the scenario lowered itself is restored, and only upwards."""
    if scenario is not None:
        scenario_dir.write_text(json.dumps(scenario), encoding="utf-8")
    nx = EngineerNX(start, engineer)
    monkeypatch.setattr(runner, "find_dev_panel", lambda: 4242)
    monkeypatch.setattr(runner, "press_panel", nx.click)
    server = create_server(nx, Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        record = await runner.run_scenario(
            session, scenario_dir, workspace=tmp_path, run_id="r6", build=("v2406", ""), owner="t"
        )
    assert record["passed"], record["steps"]
    assert nx.mode == engineer and nx.clicks == [] and "panel" not in record["cleanup"]


def test_repository_scenarios_cover_every_catalogue_tool_and_variant():
    covered = runner.coverage()
    for spec in catalogue.load().values():
        assert spec.name in covered, f"{spec.name} has no acceptance scenario"
        missing = set(spec.variant_values()) - covered[spec.name]
        assert not missing, f"{spec.name}: variants without a scenario: {sorted(missing)}"


REFUSALS = {"NX_LICENSE_UNAVAILABLE", "NX_BUILD_FAILED", "NX_IMPORT_FAILED", "NX_EXPORT_FAILED"}


def test_refusal_probes_credit_no_variant():
    """Only a scenario in which a variant succeeds may cover it; probes cover nothing."""
    for path in runner.scenario_files():
        data = runner.load_scenario(path)
        if "license" in path.name or "probe" in path.name:
            assert data["covers"] == {}, f"{path.name}: a refusal probe must cover nothing"
        for tool, variants in data["covers"].items():
            checked = [
                s["expect"] for s in data["steps"] if s.get("call") == tool and s.get("expect")
            ]
            if variants and checked:
                assert not all(
                    e.get("error.code") in REFUSALS or e.get("error.category") == "unavailable"
                    for e in checked
                ), f"{path.name}: {tool} {variants} only expect refusals"


def test_repository_scenarios_parse_and_live_in_domain_folders():
    files = runner.scenario_files()
    assert files and all(f.parent.name in catalogue.DOMAINS for f in files)
    for path in files:
        data = runner.load_scenario(path)
        assert any(step.get("expect") for step in data["steps"]), path
        assert set(data["covers"]) <= set(catalogue.load()), path


def test_committed_evidence_folds_back_to_the_committed_files(tmp_path):
    """src/nx_mcp/evidence is exactly what the acceptance evidence files produce."""
    folded, problems = runner.fold(runner.EVIDENCE, tmp_path)
    assert problems == []
    committed = Path(runner.core_evidence.EVIDENCE_DIR)
    for path in committed.glob("*.json"):
        assert json.loads(path.read_text()) == json.loads((tmp_path / path.name).read_text())
    assert {p.name for p in committed.glob("*.json")} == {p.name for p in tmp_path.glob("*.json")}


class AssemblyNX(FakeNX):
    """Scenario parts in an assembly: a used prototype refuses to close, and closing
    any part renumbers the part IDs (stale IDs are refused)."""

    def __init__(self, parents, locked=()):
        super().__init__()
        self.parents, self.locked, self.generation = parents, set(locked), 0
        for path in ["smoke/top.prt", *parents]:  # assembly first: closing reverses it
            if path not in [p["path"] for p in self.parts]:
                self.parts.append(self.row(path))
        self.renumber()

    def renumber(self):
        for p in self.parts[1:]:  # the engineer's part keeps its ID
            p["part"]["id"] = f"obj_{p['path'][6:-4]}_part{self.generation}"

    @staticmethod
    def refuse(code, message, **details):
        error = {"code": code, "category": "blocked", "message": message, "details": details}
        error["nx_code"] = 3520018 if code == "NX_READ_ONLY" else None
        envelope = {"ok": False, "tool": "nx_close_part", "mutation_outcome": "not_started"}
        raise NXError(code, message, details={"envelope": {**envelope, "error": error}})

    async def call(self, method, params):
        if method != "nx_close_part":
            return await super().call(method, params)
        self.calls.append((method, params))
        part = next((p for p in self.parts if p["part"]["id"] == params["part"]), None)
        if part is None:
            self.refuse("NX_STALE_OBJECT", "The part ID is stale")
        loaded = {p["path"] for p in self.parts}
        if part["path"] in self.locked:
            self.refuse("NX_READ_ONLY", "The part is locked")
        if self.parents.get(part["path"]) in loaded:
            self.refuse(
                "NX_PART_IN_USE", "A loaded assembly uses this part",
                parent_assemblies=[self.parents[part["path"]]],
            )  # fmt: skip
        self.parts.remove(part)
        self.generation += 1
        self.renumber()
        return self.ok(method, {"closed": [], "closed_count": 1})


async def test_cleanup_closes_assemblies_before_their_components(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")  # registers nx_close_part
    # top uses sub, sub uses pin: reverse load order tries pin, then sub, first.
    nx = AssemblyNX({"smoke/sub.prt": "smoke/top.prt", "smoke/pin.prt": "smoke/sub.prt"})
    before = [nx.parts[0]]
    server = create_server(nx, Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        report = await runner.close_new_parts(session, before)
    assert not report["failed"], report["failed"]
    assert report["closed"] == ["smoke/top.prt", "smoke/sub.prt", "smoke/pin.prt"]
    assert [p["path"] for p in nx.parts] == ["C:/eng/bracket.prt"]
    # Pass 2 closes the refusing parent (sub) before its prototype, with fresh IDs.
    closes = [c[1]["part"] for c in nx.calls if c[0] == "nx_close_part"]
    assert closes == [
        "obj_pin_part0",
        "obj_sub_part0",
        "obj_top_part0",
        "obj_sub_part1",
        "obj_pin_part2",
    ]


async def test_cleanup_reports_why_a_part_stays_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    nx = AssemblyNX({"smoke/pin.prt": "smoke/top.prt"}, locked={"smoke/top.prt"})
    server = create_server(nx, Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        report = await runner.close_new_parts(session, [nx.parts[0]])
    assert report["closed"] == []
    assert report["failed"] == [
        "could not close smoke/top.prt: NX_READ_ONLY: The part is locked (nx_code 3520018)",
        "could not close smoke/pin.prt: NX_PART_IN_USE: A loaded assembly uses this part; "
        "used by smoke/top.prt",
    ]
    closes = [c for c in nx.calls if c[0] == "nx_close_part"]
    assert len(closes) == 2 * runner.CLOSE_PASSES  # bounded retries


async def test_cleanup_restores_only_the_runs_own_display_part(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    nx = FakeNX()
    own = nx.row("smoke/dev/kept.prt", display=True)
    nx.parts = [nx.row("C:/eng/bracket.prt"), own]
    before = [dict(p) for p in nx.parts]
    nx.parts.append(nx.row("smoke/dev/new.prt", display=True))
    nx.parts[1]["display"] = nx.parts[1]["work"] = False
    server = create_server(nx, Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        report = await runner.close_new_parts(
            session, before, own_dir=tmp_path / "smoke" / "dev", workspace=tmp_path
        )
        assert report["closed"] == ["smoke/dev/new.prt"] and report["restored"] == [own["path"]]
        assert nx.calls[-1] == ("nx_activate_part", nx.calls[-1][1])
        assert nx.calls[-1][1]["part"] == own["part"]["id"]
        # Another owner's directory: nothing is displayed again.
        nx.calls.clear()
        nx.parts.append(nx.row("smoke/dev/new.prt", display=True))
        report = await runner.close_new_parts(
            session, before, own_dir=tmp_path / "smoke" / "other", workspace=tmp_path
        )
    assert report["not_restored"] == own["path"] and "restored" not in report
    assert [c[0] for c in nx.calls] == ["nx_close_part"]


async def test_cleanup_reactivates_by_fresh_ids_and_skips_parts_the_steps_closed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    nx = FakeNX()
    own = nx.row("smoke/dev/kept.prt", display=True)
    nx.parts = [nx.row("C:/eng/bracket.prt"), own]
    before = [dict(p, part=dict(p["part"])) for p in nx.parts]
    nx.parts.append(nx.row("smoke/dev/new.prt", display=True))
    own["display"] = own["work"] = False
    own["part"]["id"] = "p:77"  # NX renewed the kept part's ID since the run started
    server = create_server(nx, Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        report = await runner.close_new_parts(
            session, before, own_dir=tmp_path / "smoke" / "dev", workspace=tmp_path
        )
        assert not report["failed"] and report["restored"] == [own["path"]]
        assert nx.calls[-1] == ("nx_activate_part", nx.calls[-1][1])
        assert nx.calls[-1][1]["part"] == "p:77"  # not the stale ID listed before the run
        # A scenario that closed the run's own part itself: nothing to re-activate.
        nx.calls.clear()
        nx.parts = [nx.row("C:/eng/bracket.prt")]
        report = await runner.close_new_parts(
            session, before, own_dir=tmp_path / "smoke" / "dev", workspace=tmp_path
        )
    assert report["failed"] == [] and "restored" not in report and nx.calls == []


def test_inside_resolves_relative_and_absolute_paths(tmp_path):
    own = tmp_path / "smoke" / "r1"
    assert runner._inside("smoke/r1/a.prt", own, tmp_path)
    assert runner._inside(str(own / "a.prt"), own, None)
    assert not runner._inside("smoke/r10/a.prt", own, tmp_path)
    assert not runner._inside("C:/eng/bracket.prt", own, tmp_path)
    assert not runner._inside(None, own, tmp_path) and not runner._inside("a.prt", None, None)


def test_close_failure_without_a_reason():
    assert runner.close_failure("a.prt", None) == "could not close a.prt: no reason given"

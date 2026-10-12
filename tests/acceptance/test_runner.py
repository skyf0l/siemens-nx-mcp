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
    return path


async def test_scenario_runs_through_the_gateways_and_writes_evidence(tmp_path, scenario_dir):
    nx = FakeNX()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    server = create_server(nx, Workspace(workspace), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        record, details = await runner.run_scenario(
            session,
            scenario_dir,
            workspace=workspace,
            run_id="r1",
            build=("v2406", "NX 2406.1700"),
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
    assert all(s["result_sha256"] for s in details["steps"])
    # Cleanup: the scenario part is closed unsaved by ID. The engineer's part is never
    # displayed again by the run (re-displaying a part can switch NX's application).
    assert details["cleanup"]["closed"] == [part] and not details["cleanup"]["failed"]
    assert details["cleanup"]["not_restored"] == "C:/eng/bracket.prt"
    method, params = nx.calls[-1]
    assert method == "nx_close_part" and params["save"] is False
    assert params["part"] == nx.row(part)["part"]["id"]
    assert "nx_activate_part" not in [c[0] for c in nx.calls]
    assert [p["path"] for p in nx.parts] == ["C:/eng/bracket.prt"]

    # Evidence holds the deterministic proof only; run details go to the run log.
    assert list(record) == [
        "format", "scenario", "scenario_sha256", "domain", "covers", "nx_version",
        "nx_build", "sources", "recorded", "passed", "checks", "failed_checks", "steps",
    ]  # fmt: skip
    assert record["format"] == runner.EVIDENCE_FORMAT
    assert all(list(s) == ["index", "call", "setup", "passed", "checks"] for s in record["steps"])
    assert details["part"] == part and details["finished"] and details["cleanup"]["closed"]

    evidence_root = tmp_path / "evidence"
    written, changed = runner.write_evidence(record, evidence_root)
    assert changed and written == evidence_root / "v2406" / "modeling" / "fake.json"
    assert b"\r" not in written.read_bytes()
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
    failed, _ = runner.write_evidence({**record, "passed": False}, evidence_root)
    assert failed.name == "fake.last-failure.json"


async def test_a_failing_expectation_fails_the_scenario(tmp_path, scenario_dir):
    broken = json.loads(json.dumps(SCENARIO))
    broken["steps"][2]["expect"]["data.origin.2"] = {"close": 30, "tol": 1e-9}
    scenario_dir.write_text(json.dumps(broken), encoding="utf-8")
    server = create_server(FakeNX(), Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        record, details = await runner.run_scenario(
            session, scenario_dir, workspace=tmp_path, run_id="r2", build=("v2406", "")
        )
    assert not record["passed"] and record["failed_checks"] == 1
    assert "close 30" in record["steps"][2]["failures"][0]
    assert details["steps"][2]["failures"] == record["steps"][2]["failures"]


async def test_parts_are_closed_when_a_setup_step_fails(tmp_path, scenario_dir):
    broken = json.loads(json.dumps(SCENARIO))
    broken["steps"].insert(1, {"call": "nx_create_datum_plane", "args": {"method": "x"}})
    scenario_dir.write_text(json.dumps(broken), encoding="utf-8")
    nx = FakeNX()
    server = create_server(nx, Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        record, details = await runner.run_scenario(
            session, scenario_dir, workspace=tmp_path, run_id="r3", build=("v2406", "")
        )
    assert not record["passed"] and len(record["steps"]) == 2  # stopped at the failed setup
    assert len(details["cleanup"]["closed"]) == 1
    assert [p["path"] for p in nx.parts] == ["C:/eng/bracket.prt"]


async def test_cleanup_keep_leaves_parts_loaded(tmp_path, scenario_dir):
    scenario_dir.write_text(json.dumps({**SCENARIO, "cleanup": "keep"}), encoding="utf-8")
    nx = FakeNX()
    server = create_server(nx, Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        record, details = await runner.run_scenario(
            session, scenario_dir, workspace=tmp_path, run_id="r4", build=("v2406", "")
        )
    assert details["cleanup"] == {"mode": "keep", "closed": [], "failed": []}
    assert len(nx.parts) == 2


async def test_a_second_identical_run_leaves_the_evidence_file_byte_identical(
    tmp_path, scenario_dir, monkeypatch
):
    evidence_root = tmp_path / "evidence"
    times = iter(f"2026-10-10T12:00:{n:02}Z" for n in range(60))
    monkeypatch.setattr(runner, "utc_now", lambda: next(times))
    outputs = []
    for run_id in ("20261010-120000-aaaaaa", "20261011-090000-bbbbbb"):
        workspace = tmp_path / run_id  # another run, another workspace, another part path
        workspace.mkdir()
        server = create_server(FakeNX(), Workspace(workspace), surface="agent")
        async with create_connected_server_and_client_session(server) as session:
            record, _ = await runner.run_scenario(
                session, scenario_dir, workspace=workspace, run_id=run_id,
                build=("v2406", "2406.9160"),
            )  # fmt: skip
        path, changed = runner.write_evidence(record, evidence_root)
        outputs.append((record, changed, path.read_bytes(), path.stat().st_mtime_ns))
    (first, wrote, data, mtime), (second, rewrote, again, mtime_again) = outputs
    assert first["recorded"] != second["recorded"]  # only the time differs
    assert wrote and not rewrote and data == again and mtime == mtime_again
    assert json.loads(data)["recorded"] == first["recorded"]
    # A real change (another NX build) rewrites the file, with the new time.
    path, changed = runner.write_evidence({**second, "nx_build": "2406.9200"}, evidence_root)
    assert changed and json.loads(path.read_bytes())["recorded"] == second["recorded"]


async def test_failure_evidence_keeps_the_error_without_local_paths(tmp_path, scenario_dir):
    broken = json.loads(json.dumps(SCENARIO))
    broken["steps"].insert(1, {"call": "nx_create_datum_plane", "args": {"method": "x"}})
    scenario_dir.write_text(json.dumps(broken), encoding="utf-8")
    server = create_server(FakeNX(), Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        record, details = await runner.run_scenario(
            session, scenario_dir, workspace=tmp_path, run_id="r7", build=("v2406", "")
        )
    step = record["steps"][1]
    assert not step["passed"] and step["error"]["code"] and step["error"]["message"]
    assert step["failures"][0].startswith(f"unexpected error {step['error']['code']}")
    assert details["steps"][1]["result_sha256"] and "seconds" not in step
    path, _ = runner.write_evidence(record, tmp_path / "evidence")
    assert path.name == "fake.last-failure.json"


def test_scrubber_replaces_local_paths_in_every_spelling(tmp_path):
    workspace = Path("C:/Users/someone/NX_MCP_WORKSPACE")
    scrub = runner.scrubber(
        [(workspace / "smoke" / "20261010-x", "$run"), (workspace, "$workspace"),
         (Path("C:/Users/someone"), "~"), ("20261010-x", "$run_id")]
    )  # fmt: skip
    text = (
        r"cannot open C:\Users\someone\NX_MCP_WORKSPACE\smoke\20261010-x\a.prt; "
        "c:/users/someone/nx_mcp_workspace/b.prt; "
        r"repr 'C:\\Users\\someone\\x.stp'; run 20261010-x"
    )
    assert scrub(text) == (
        r"cannot open $run\a.prt; $workspace/b.prt; repr '~\\x.stp'; run $run_id"
    )


def test_the_run_log_keeps_the_run_details_outside_the_repository(tmp_path):
    log = {"run_id": "20261010-1-abc", "owner": "me", "scenarios": [{"part": "C:/x.prt"}]}
    path = runner.write_run_log(tmp_path, log)
    assert path == tmp_path / ".nx-mcp" / "acceptance-runs" / "20261010-1-abc.json"
    assert json.loads(path.read_text(encoding="utf-8")) == log


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


def test_fold_is_deterministic(tmp_path):
    """Identical evidence folds to byte-identical files; a re-fold touches nothing."""
    first, second = tmp_path / "a", tmp_path / "b"
    runner.fold(runner.EVIDENCE, first)
    runner.fold(runner.EVIDENCE, second)
    files = sorted(p.name for p in first.glob("*.json"))
    assert files and files == sorted(p.name for p in second.glob("*.json"))
    for name in files:
        data = (first / name).read_bytes()
        assert data == (second / name).read_bytes() and b"\r" not in data
    stamps = {name: (first / name).stat().st_mtime_ns for name in files}
    runner.fold(runner.EVIDENCE, first)
    assert stamps == {name: (first / name).stat().st_mtime_ns for name in files}
    # ``recorded`` comes from the evidence files, never from the time of the fold.
    recorded = {
        json.loads(p.read_text(encoding="utf-8"))["recorded"]
        for p in runner.EVIDENCE.glob("*/*/*.json")
        if not p.name.endswith(".last-failure.json")
    }
    folded = runner.core_evidence.load(first)
    assert {e["recorded"] for builds in folded.values() for e in builds.values()} <= recorded


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
        report = await runner.close_new_parts(
            session, before, own_dir=tmp_path / "smoke", workspace=tmp_path
        )
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
        report = await runner.close_new_parts(
            session, [nx.parts[0]], own_dir=tmp_path / "smoke", workspace=tmp_path
        )
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
        # Another owner's directory: its new part is never closed, nothing displayed again.
        nx.calls.clear()
        nx.parts.append(nx.row("smoke/dev/new.prt", display=True))
        report = await runner.close_new_parts(
            session, before, own_dir=tmp_path / "smoke" / "other", workspace=tmp_path
        )
    assert report["closed"] == [] and "restored" not in report
    assert "nx_close_part" not in [c[0] for c in nx.calls]


async def test_cleanup_never_closes_parts_loaded_outside_the_run_folder(tmp_path, monkeypatch):
    # Another session's agent (or the engineer) loads parts in the same NX during a run.
    monkeypatch.setenv("NX_MCP_DEV", "1")
    nx = FakeNX()
    nx.parts = [nx.row("C:/eng/bracket.prt")]
    before = [dict(p) for p in nx.parts]
    nx.parts += [
        nx.row("smoke/run1/mine.prt"),
        nx.row("cars/bugatti_chiron.prt"),
        nx.row("smoke/other/theirs.prt"),
    ]
    server = create_server(nx, Workspace(tmp_path), surface="agent")
    async with create_connected_server_and_client_session(server) as session:
        report = await runner.close_new_parts(
            session, before, own_dir=tmp_path / "smoke" / "run1", workspace=tmp_path
        )
        assert report["closed"] == ["smoke/run1/mine.prt"]
        # Without the run's folder nothing is closed.
        nx.parts.append(nx.row("smoke/run1/late.prt"))
        assert (await runner.close_new_parts(session, before))["closed"] == []
    assert [p["path"] for p in nx.parts] == [
        "C:/eng/bracket.prt",
        "cars/bugatti_chiron.prt",
        "smoke/other/theirs.prt",
        "smoke/run1/late.prt",
    ]


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


def test_file_hashes_ignore_line_endings(tmp_path):
    """A Windows (CRLF) checkout and an LF one record the same scenario and source hashes."""
    lf, crlf = tmp_path / "lf.json", tmp_path / "crlf.json"
    lf.write_bytes(b'{\n  "steps": []\n}\n')
    crlf.write_bytes(b'{\r\n  "steps": []\r\n}\r\n')
    assert runner.file_sha256(lf) == runner.file_sha256(crlf)


def test_expect_values_take_saved_values_and_keep_other_dollar_text():
    saved = {"link": {"data": {"part": "p:4", "length": 12.5}}}
    rules = {
        "data.part": "$link.data.part",
        "data.length": {"close": "$link.data.length", "tol": 1e-6},
        "data.columns.0.field": "$~C",  # an NX text code, not a saved value
        "data.note": "$$link",
    }
    assert runner.resolve_expect(rules, saved, {}) == {
        "data.part": "p:4",
        "data.length": {"close": 12.5, "tol": 1e-6},
        "data.columns.0.field": "$~C",
        "data.note": "$link",
    }
    with pytest.raises(KeyError):
        runner.resolve_expect({"data.part": "$nope.data"}, saved, {})


def test_blocking_parts_are_left_agent_parts_and_parts_outside_smoke(tmp_path):
    rows = [
        {"path": "smoke/run-1/a.prt", "owned": True},  # an agent part left loaded
        {"path": "smoke/run-1/b.prt", "owned": False},  # a smoke part of an earlier session
        {"path": "projects/housing.prt", "owned": False},  # the engineer's
        {"path": str(tmp_path / "smoke" / "x" / "c.prt"), "owned": False},
    ]
    assert runner.blocking_parts(rows, tmp_path) == ["smoke/run-1/a.prt", "projects/housing.prt"]
    assert runner.blocking_parts([], tmp_path) == []
    assert runner.blocking_parts(None, tmp_path) == []


async def test_an_engineer_step_runs_its_snippet_through_the_development_bridge(monkeypatch):
    """``{"call": "engineer", "args": {"code": ...}}`` edits NX as the engineer would: the
    snippet runs through ``nx_dev_python`` with the host's engineer-change check after it."""
    sent: list = []

    class Client:
        def __init__(self, path):
            sent.append(path)

        async def call(self, method, params):
            sent.append((method, params["code"]))
            return {"status": "success", "stdout": "", "result": "{'noticed': True, 'value': None}"}

    import nx_mcp.bridge

    monkeypatch.setattr(nx_mcp.bridge, "DescriptorBridgeClient", Client)
    monkeypatch.setenv("NX_MCP_BRIDGE_DESCRIPTOR", "bridge.json")
    steps = [{"call": "engineer", "args": {"code": "x = 1"}, "expect": {"data.noticed": True}}]
    (result,) = await runner.run_steps(None, steps, {}, label="t")
    assert result.passed and result.call == "engineer", result.failures
    method, code = sent[1]
    assert method == "nx_dev_python" and "x = 1" in code and "_check_engineer_change" in code


def test_an_engineer_snippet_sees_run_as_the_run_directory():
    """``$run`` inside an engineer step's code is the run directory, as in arguments."""
    args = {"code": "path = r'$run\\tp\\a.prt'"}
    got = runner.engineer_args(args, {"$run": "C:\\ws\\smoke\\r1"})
    assert got["code"] == "path = r'C:\\ws\\smoke\\r1\\tp\\a.prt'"
    assert runner.engineer_args(args, {}) is args


async def test_a_direct_step_calls_the_listed_core_tool_itself():
    """``"direct": true``: the core tool as its own MCP tool, arguments as given."""
    from types import SimpleNamespace

    calls = []

    class Session:
        async def call_tool(self, name, args):
            calls.append((name, args))
            body = {"ok": True, "tool": name, "mutation_outcome": "not_applicable", "data": {}}
            return SimpleNamespace(structuredContent=body, content=[])

    out = await runner.call_tool(
        Session(), "nx_check_model", {"part": "cover"}, intent="x", direct=True
    )
    assert calls == [("nx_check_model", {"part": "cover"})] and out["ok"]
    await runner.call_tool(Session(), "nx_check_model", {}, intent=None)
    assert calls[-1][0] == "nx_invoke_read"

"""``verify`` for volunteers on another NX build, and evidence in public terms only."""

import contextlib
import functools
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.acceptance import runner
from nx_mcp.core.errors import NXError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace
from tests.acceptance.test_runner import SCENARIO, FakeNX

# ---------------------------------------------------------------------------
# Public terms


@pytest.mark.parametrize(
    ("text", "public"),
    [
        (
            "unexpected error NX_LICENSE_UNAVAILABLE: NX refused it: FEATURE_X not found in C:/x",
            "unexpected error NX_LICENSE_UNAVAILABLE",
        ),
        ("data.origin.2: close 30 failed (actual 25.0)", "data.origin.2: close failed (wanted 30, actual 25.0)"),
        ("data.n: close [1, 2.5] failed (actual [1.0, 2.0])", "data.n: close failed (wanted [1,2.5], actual [1.0,2.0])"),
        ("data.items: count 2 failed (actual [{'name': 'bracket'}])", "data.items: count failed (wanted 2, actual 1)"),
        ("data.part: equals 'a.prt' failed (actual 'C:/Users/me/b.prt')", "data.part: equals failed"),
        ("mutation_outcome: equals 'committed' failed (actual 'rolled_back')", "mutation_outcome: equals failed"),
        ("data.on: equals True failed (actual 1)", "data.on: equals failed"),
        ("changes.created: missing (wanted {'count_kind': {'annotation': 1}})", "changes.created: missing"),
        ("argument: $datum.data.id: no such path in the saved result 'datum'", "argument: $datum.data.id unresolved"),
        ("call failed: TimeoutError('C:/Users/me/x')", "call failed: TimeoutError"),
        ("something NX said about secret.prt", "check failed"),
    ],
)  # fmt: skip
def test_failures_keep_only_codes_paths_operators_and_numbers(text, public):
    assert runner.public_failure(text) == public
    assert runner.public_failure(public) == public  # idempotent


def test_errors_keep_only_code_category_field_and_a_missing_member():
    problem = {
        "code": "NX_VERSION_UNSUPPORTED",
        "category": "unavailable",
        "field": "/method",
        "message": "secret.prt: FEATURE_X",
        "nx_code": 948801,
        "hint": "ask",
        "details": {"member": "Features.FooBuilder.Bar", "license": "FEATURE_X"},
    }
    assert runner.public_error(problem) == {
        "code": "NX_VERSION_UNSUPPORTED",
        "category": "unavailable",
        "field": "/method",
        "member": "Features.FooBuilder.Bar",
    }
    odd = {"code": "lower case: text", "category": "Bad Category", "field": "C:\\x y"}
    assert runner.public_error(odd) == {"code": "NX_ERROR"}
    other = {"code": "NX_ERROR", "details": {"member": "X"}}
    assert runner.public_error(other) == {"code": "NX_ERROR"}  # a member only when missing


def _failed_record(code="NX_EXPORT_FAILED"):
    return {
        "format": runner.EVIDENCE_FORMAT,
        "scenario": "acceptance/parts_files/x.json",
        "scenario_sha256": "0" * 64,
        "domain": "parts_files",
        "covers": {"nx_export_model": ["step"]},
        "nx_version": "v2412",
        "nx_build": "2412.8700",
        "sources": {},
        "recorded": "2026-10-16T00:00:00Z",
        "passed": False,
        "checks": 1,
        "failed_checks": 1,
        "steps": [
            {"index": 1, "call": "nx_create_part", "setup": True, "passed": True, "checks": 0},
            {
                "index": 2,
                "call": "nx_export_model",
                "setup": False,
                "passed": False,
                "checks": 1,
                "error": {"code": code, "message": "wrote nothing to C:/Users/me/ws/a.stp"},
                "failures": [f"unexpected error {code}: wrote nothing to C:/Users/me/ws/a.stp"],
            },
        ],
    }


def test_a_licence_refusal_is_never_written_and_other_failures_are_public(tmp_path):
    path, changed = runner.write_evidence(_failed_record("NX_LICENSE_UNAVAILABLE"), tmp_path)
    assert path is None and not changed and not list(tmp_path.rglob("*.json"))
    path, changed = runner.write_evidence(_failed_record(), tmp_path)
    assert changed and path.name == "x.last-failure.json"
    text = path.read_text(encoding="utf-8")
    assert "C:/" not in text and "wrote nothing" not in text
    step = json.loads(text)["steps"][1]
    assert step["error"] == {"code": "NX_EXPORT_FAILED"}
    assert step["failures"] == ["unexpected error NX_EXPORT_FAILED"]


def test_existing_failure_records_are_scrubbed_or_removed(tmp_path):
    folder = tmp_path / "v2412" / "parts_files"
    folder.mkdir(parents=True)
    old = folder / "x.last-failure.json"
    old.write_bytes(runner.dump(_failed_record()))
    licence = folder / "y.last-failure.json"
    licence.write_bytes(runner.dump(_failed_record("NX_LICENSE_UNAVAILABLE")))
    changes = runner.scrub_failure_records(tmp_path)
    assert changes == [
        "scrubbed v2412/parts_files/x.last-failure.json",
        "removed v2412/parts_files/y.last-failure.json (a licence refusal is never recorded)",
    ]
    assert not licence.exists() and "C:/" not in old.read_text(encoding="utf-8")
    assert runner.scrub_failure_records(tmp_path) == []  # idempotent


# ---------------------------------------------------------------------------
# verify


class VolunteerNX(FakeNX):
    """A volunteer's NX: nothing loaded; ``refuse`` answers a tool with a licence refusal."""

    def __init__(self, refuse=None):
        super().__init__()
        self.parts = []
        self.refuse = refuse

    async def call(self, method, params):
        if method == self.refuse:
            raise NXError(
                "NX_LICENSE_UNAVAILABLE",
                "NX refused FEATURE_X",
                details={
                    "mutation_outcome": "rolled_back",
                    "envelope": {
                        "ok": False,
                        "tool": method,
                        "mutation_outcome": "rolled_back",
                        "error": {"code": "NX_LICENSE_UNAVAILABLE", "category": "unavailable",
                                  "message": "NX refused FEATURE_X"},
                    },
                },
            )  # fmt: skip
        return await super().call(method, params)


@pytest.fixture
def volunteer(tmp_path, monkeypatch):
    """Two modeling scenarios (one plays the engineer), a workspace and a fake sidecar."""
    root = tmp_path / "repo"
    (root / "acceptance" / "modeling").mkdir(parents=True)
    (root / "acceptance" / "modeling" / "one.json").write_text(json.dumps(SCENARIO), "utf-8")
    engineer = {**SCENARIO, "steps": [{"call": "engineer", "args": {"code": "pass"}}]}
    (root / "acceptance" / "modeling" / "two.json").write_text(json.dumps(engineer), "utf-8")
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "SCENARIOS", root / "acceptance")
    monkeypatch.setenv("NX_MCP_DEV", "1")
    monkeypatch.setattr(runner, "DevLock", functools.partial(runner.DevLock, beat=0.05))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = {"nx": VolunteerNX(), "bridge": {"build_tested": False}, "workspace": workspace}

    @contextlib.asynccontextmanager
    async def sidecar(ws, caller=None):
        server = create_server(state["nx"], Workspace(ws), surface="agent")
        async with create_connected_server_and_client_session(server) as session:
            yield session

    async def bridge_status(session):
        return ("v2412", "2412.8700"), state["bridge"]

    async def no_development():
        return False

    monkeypatch.setattr(runner, "sidecar", sidecar)
    monkeypatch.setattr(runner, "bridge_status", bridge_status)
    monkeypatch.setattr(runner, "engineer_steps_available", no_development)
    state["evidence"] = root / "acceptance" / "evidence"
    state["out"] = tmp_path / "folded"
    state["verify"] = functools.partial(
        runner.verify,
        workspace=workspace,
        owner="volunteer",
        evidence_root=state["evidence"],
        out_dir=state["out"],
    )
    return state


async def test_verify_runs_folds_and_lists_the_files_to_contribute(volunteer, capsys):
    assert await volunteer["verify"](["modeling"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    files = lines[lines.index("Files to include in a contribution (1):") + 1 :][:1]
    assert files == ["  acceptance/evidence/v2412/modeling/one.json"]
    assert "Summary: 1 passed, 0 failed, 1 skipped (0 NX refused a licence, 1 need" in out
    assert (volunteer["evidence"] / "v2412" / "modeling" / "one.json").is_file()
    folded = json.loads((volunteer["out"] / "modeling.json").read_text(encoding="utf-8"))
    assert folded["nx_create_datum_plane"]["v2412"]["variants"] == ["offset"]
    summary = runner.read_verify_summary(volunteer["workspace"])
    assert summary["nx_version"] == "v2412" and summary["nx_build"] == "2412.8700"
    assert (summary["passed"], summary["failed"], summary["skipped"]) == (1, 0, 1)
    assert summary["complete"] and summary["failed_scenarios"] == []
    assert not (volunteer["workspace"] / ".nx-mcp" / "dev.lock").exists()  # released


async def test_verify_refuses_beside_parts_it_did_not_create(volunteer, capsys):
    volunteer["nx"].parts = [FakeNX.row("C:/Users/me/secret/bracket.prt", display=True)]
    assert await volunteer["verify"](["modeling"]) == 2
    out = capsys.readouterr().out
    assert "REFUSED: 1 part(s) are loaded" in out and "bracket" not in out
    assert not volunteer["evidence"].exists() and volunteer["nx"].calls == []


async def test_verify_refuses_when_nx_mcp_only_reads(volunteer, capsys):
    volunteer["bridge"]["read_only"] = "NX_MCP_UNTESTED=read_only"
    assert await volunteer["verify"](["modeling/one"]) == 2
    assert "REFUSED" in capsys.readouterr().out and not volunteer["evidence"].exists()


async def test_a_licence_refusal_is_skipped_and_never_recorded(volunteer, capsys):
    volunteer["nx"].refuse = "nx_create_datum_plane"
    assert await volunteer["verify"](["modeling/one"]) == 0
    out = capsys.readouterr().out
    assert "SKIPPED: NX refused a licence" in out and "FEATURE_X" not in out
    assert "Summary: 0 passed, 0 failed, 1 skipped (1 NX refused a licence" in out
    assert not list(volunteer["evidence"].rglob("*.json"))
    summary = runner.read_verify_summary(volunteer["workspace"])
    assert "license" not in json.dumps(summary).lower()


async def test_a_failure_is_recorded_and_summarised_in_public_terms(volunteer, capsys):
    broken = json.loads(json.dumps(SCENARIO))
    broken["steps"][2]["expect"]["data.origin.2"] = {"close": 30, "tol": 1e-9}
    (runner.SCENARIOS / "modeling" / "one.json").write_text(json.dumps(broken), "utf-8")
    assert await volunteer["verify"](["modeling/one"]) == 1
    out = capsys.readouterr().out
    assert "data.origin.2: close failed (wanted 30, actual 25)" in out
    assert "  acceptance/evidence/v2412/modeling/one.last-failure.json" in out
    summary = runner.read_verify_summary(volunteer["workspace"])
    assert summary["failed_scenarios"] == [{"scenario": "modeling/one", "codes": []}]

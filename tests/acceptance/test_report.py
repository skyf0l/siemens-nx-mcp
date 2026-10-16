"""The field report a volunteer sends and the engineer's local capabilities page."""

import getpass
import json
import re
import socket

import pytest

from nx_mcp import report
from nx_mcp.acceptance import runner
from nx_mcp.core import catalogue, evidence


def _tested_tool():
    """A tool tested on v2406 (whole tool) and one with variants tested there."""
    specs = catalogue.load().values()
    plain = next(s for s in specs if not s.variants and evidence.available(s, "v2406"))
    varied = next(
        s
        for s in specs
        if isinstance(s.variants, str) and len(evidence.variants_for(s, "v2406")) >= 2
    )
    return plain, varied


def _status(**bridge):
    base = {
        "state": "connected",
        "answered_from": "nx",
        "nx_version": "v2412",
        "nx_build": "2412.8700",
        "build_tested": False,
        "license_unavailable": [],
    }
    return {"ok": True, "data": {"bridge": {**base, **bridge}}}


def test_the_field_report_holds_versions_and_tokens_only(tmp_path):
    plain, varied = _tested_tool()
    variant = evidence.variants_for(varied, "v2406")[0]
    status = _status(
        version_unsupported=[
            {
                "tool": varied.name,
                "variant": variant,
                "build": "2412.8700",
                "member": "Features.FooBuilder.Bar",
                "since": "2026-10-16T10:00:00",
                "message": f"failed in {tmp_path}\\secret.prt",
                "exception": "AttributeError",
            },
            {"tool": "x; rm", "member": "a"},  # not a token: left out
        ],
        license_unavailable=[{"tool": plain.name, "license": "FEATURE_X", "nx_code": 948801}],
        self_test={"state": "passed", "reason": "secret", "at": "x"},
    )
    verify = {
        "nx_version": "v2412",
        "nx_build": "2412.8700",
        "finished": "2026-10-16T10:00:00Z",
        "complete": True,
        "scenarios": 3,
        "passed": 1,
        "failed": 1,
        "skipped": 1,
        "failed_scenarios": [
            {"scenario": "modeling/hole", "codes": ["NX_VERSION_UNSUPPORTED", "bad code"],
             "members": ["Features.HoleBuilder.X", "C:/x y"]},
            {"scenario": "C:/Users/me/x", "codes": []},
        ],
    }  # fmt: skip
    content = report.field_report(status, verify, today="2026-10-16")
    assert content["nx"] == {
        "connected": True,
        "version": "2412.8700",
        "build": "v2412",
        "build_tested": False,
        "self_test": "passed",
        "read_only": False,
    }
    assert content["version_unsupported"] == [
        {
            "tool": varied.name,
            "variant": variant,
            "member": "Features.FooBuilder.Bar",
            "exception": "AttributeError",
        }
    ]
    assert content["verify"]["failed_scenarios"] == [
        {
            "scenario": "modeling/hole",
            "codes": ["NX_VERSION_UNSUPPORTED"],
            "members": ["Features.HoleBuilder.X"],
        }
    ]
    assert (content["verify"]["passed"], content["verify"]["skipped"]) == (1, 1)
    path = report.write_report(tmp_path, content)
    assert path == tmp_path / "nx-mcp-report-v2412-2026-10-16.json"
    text = path.read_text(encoding="utf-8")
    for secret in ("FEATURE_X", "948801", "nx_code", "licen", "secret", "message", str(tmp_path)):
        assert secret.lower() not in text.lower(), secret
    assert json.loads(text) == content  # nothing to scrub: tokens only


def test_local_names_left_in_a_report_are_replaced(tmp_path):
    user, host = getpass.getuser(), socket.gethostname()
    text = f'{{"a": "{tmp_path}\\\\x", "b": "{user}", "c": "{host}", "d": "build_tested"}}'
    scrubbed = report.scrub_text(text, tmp_path)
    assert str(tmp_path).lower() not in scrubbed.lower() and "<workspace>" in scrubbed
    if len(user) >= 3:
        assert re.search(rf"(?<![\w]){re.escape(user)}(?![\w])", scrubbed, re.I) is None
    assert "build_tested" in scrubbed


def test_the_report_command_writes_the_file_and_says_what_to_do(tmp_path, capsys):
    runner.write_verify_summary(tmp_path, {"nx_version": "v2412", "passed": 2, "failed": 0})
    code = report.main(["--workspace", str(tmp_path)], status_reader=lambda ws: _status())
    assert code == 0
    out = capsys.readouterr().out
    written = next(tmp_path.glob("nx-mcp-report-v2412-*.json"))
    assert str(written) in out and "check it, then send it to the maintainer" in out.lower()
    assert json.loads(written.read_text(encoding="utf-8"))["verify"]["passed"] == 2


def test_without_nx_the_report_says_so_and_the_page_is_refused(tmp_path, capsys):
    down = {"ok": True, "data": {"bridge": {"state": "down", "answered_from": "sidecar"}}}
    assert report.main(["--workspace", str(tmp_path)], status_reader=lambda ws: down) == 0
    content = json.loads(next(tmp_path.glob("nx-mcp-report-unknown-*.json")).read_text("utf-8"))
    assert content["nx"]["connected"] is False and content["nx"]["version"] is None
    code = report.main(
        ["--workspace", str(tmp_path), "--capabilities"], status_reader=lambda ws: None
    )
    assert code == 1 and not (tmp_path / report.CAPABILITIES).exists()


@pytest.mark.parametrize("build", ["2406.9160", "2412.8700"])
def test_the_capabilities_page_sorts_tools_per_domain(tmp_path, build):
    plain, varied = _tested_tool()
    tested_variants = evidence.variants_for(varied, "v2406")
    bridge = _status(
        nx_build=build,
        build_tested=build.startswith("2406"),
        version_unsupported=[{"tool": varied.name, "variant": tested_variants[0]}],
        license_unavailable=[{"tool": plain.name, "variant": None, "license": "FEATURE_X"}],
    )["data"]["bridge"]
    table = report.capabilities(bridge)
    rows = {r["tool"]: r for rows in table.values() for r in rows}
    assert rows[plain.name]["license"] == ["*"] and not rows[plain.name]["tested"]
    assert rows[varied.name]["unsupported"] == [tested_variants[0]]
    others = rows[varied.name]["tested" if build.startswith("2406") else "untested"]
    assert set(tested_variants[1:]) <= set(others)
    assert not any(evidence.tested_on(catalogue.load()[t]) == [] for t in rows)  # never offered
    path = report.write_capabilities(tmp_path, bridge, now="2026-10-16 10:00")
    assert path == tmp_path / ".nx-mcp" / "capabilities.html"
    page = path.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>") and f"<code>{varied.name}</code>" in page
    assert "FEATURE_X" not in page and "prefers-color-scheme: dark" in page

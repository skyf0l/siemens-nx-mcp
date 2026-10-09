"""Invariants every feature group must satisfy (see nx_mcp/feature_groups.py)."""

import inspect

import pytest

from nx_mcp.agent_guidance import MANIFEST
from nx_mcp.feature_groups import server_modules, tool_names
from nx_mcp.hardened import HardenedExecutor

GROUPS = server_modules()


@pytest.mark.parametrize("module", GROUPS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_group_tools_are_complete(module):
    names = tool_names(module)
    for name in names:
        schema = getattr(module, name)
        assert inspect.getdoc(schema), f"{name} needs a docstring (its MCP description)"
        handler = getattr(HardenedExecutor, "_" + name[3:], None)
        assert handler is not None, f"{name} has no HardenedExecutor handler"
        wanted = list(inspect.signature(schema).parameters)
        accepted = list(inspect.signature(handler).parameters)[1:]
        assert wanted == accepted, f"{name}: schema {wanted} != handler {accepted}"
        assert name in MANIFEST["tools"], f"{name} needs a capability manifest entry"
    assert set(names) >= module.READ_ONLY and set(names) >= module.NON_MODEL
    assert set(module.SUMMARIES) <= set(names)


def test_tool_names_are_unique_across_groups():
    seen = {}
    for module in GROUPS:
        for name in tool_names(module):
            assert name not in seen, f"{name} defined in {seen.get(name)} and {module.__name__}"
            seen[name] = module.__name__


def test_capabilities_report_evidence_for_the_running_nx_version():
    from nx_mcp.manifest import load_manifest, scope_to_version

    tools = scope_to_version(load_manifest(), "v2406")["tools"]
    assert tools["nx_extrude"]["status"] == "tested"  # verified_versions includes v2406
    assert tools["nx_datum_plane"]["status"] == "tested"  # real_NX_v2406 evidence
    assert tools["nx_pmi_datum"]["status"] == "unavailable"  # unavailable_on v2406
    assert scope_to_version(load_manifest(), "v2606")["tools"]["nx_pmi_datum"]["status"] == (
        "tested"
    )
    only_2606 = {"tools": {"t": {"status": "tested", "evidence_type": "real_NX_v2606_scoped"}}}
    entry = scope_to_version(only_2606, "v2406")["tools"]["t"]
    assert entry["status"] == "experimental" and "Not tested on NX v2406" in entry["scope"]


def test_capability_fragments_ship_as_package_data():
    # The wheel only carries listed data files; a missing fragment silently drops its tools.
    import fnmatch
    from pathlib import Path

    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    root = Path(__file__).resolve().parents[1]
    patterns = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "setuptools"
    ]["package-data"]["nx_mcp"]
    package = root / "src" / "nx_mcp"
    for fragment in (package / "capability_fragments").glob("*.json"):
        name = fragment.relative_to(package).as_posix()
        assert any(fnmatch.fnmatch(name, p) for p in patterns), name


def test_every_tool_is_proven_or_disabled_on_nx_2406():
    # NX 2406 is a supported build: no tool may stay merely "experimental" there.
    from nx_mcp.manifest import load_manifest, scope_to_version

    tools = scope_to_version(load_manifest(), "v2406")["tools"]
    unproven = sorted(n for n, e in tools.items() if e["status"] not in {"tested", "unavailable"})
    assert unproven == []


def test_native_license_failures_get_a_stable_code():
    from nx_mcp.runtime import api_error

    class NXException(Exception):
        ErrorCode = 948801

    error = api_error(NXException("License Error"))
    assert error.code == "NX_LICENSE_UNAVAILABLE" and error.nx_code == 948801
    NXException.ErrorCode = 1
    assert api_error(NXException("other")).code == "NX_API_ERROR"

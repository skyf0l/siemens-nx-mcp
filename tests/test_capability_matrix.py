"""Evidence labels must not promote local or missing evidence into native claims."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/generate_capability_matrix.py"
SPEC = importlib.util.spec_from_file_location("capability_matrix", SCRIPT)
assert SPEC and SPEC.loader
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


@pytest.mark.parametrize(
    ("status", "evidence", "expected"),
    [
        ("tested", "real_NX_v2606_scoped_and_local_boundary_tests", "Native-tested"),
        ("experimental", "real_NX_v2606", "Experimental"),
        ("tested", "local_contract_test", "Contract/sidecar-tested"),
        ("tested", "live_sidecar_and_contract_tests", "Contract/sidecar-tested"),
        ("tested", None, "Experimental"),
        ("tested", "unrecognized", "Experimental"),
        ("unavailable", "real_NX_v2606", "Unavailable"),
    ],
)
def test_classification_does_not_inflate_evidence(status, evidence, expected):
    assert matrix.classification({"status": status, "evidence_type": evidence}) == expected


def test_render_preserves_caveats_and_is_order_independent():
    manifest = {
        "revision": "fixture",
        "nx_version": "v2606",
        "bridge_protocol": 1,
        "tools": {
            "nx_z": {"status": "tested", "scope": "unknown | <unsafe>\nsecond line"},
            "nx_a": {
                "status": "tested",
                "evidence_type": "real_NX_v2606",
                "scope": "one case only",
            },
        },
        "unavailable": ["missing_feature"],
        "limitations": ["No general certification"],
    }
    first = matrix.render(manifest)
    manifest["tools"] = dict(reversed(list(manifest["tools"].items())))
    assert first == matrix.render(manifest)
    assert "unknown &#124; &lt;unsafe&gt;<br>second line" in first
    assert "one case only" in first
    assert "- missing_feature" in first
    assert "No general certification" in first


def test_default_manifest_includes_fragments_like_the_server():
    from nx_mcp.manifest import load_manifest

    manifest = matrix.load_manifest()
    assert set(manifest["tools"]) == set(load_manifest()["tools"])
    rendered = matrix.render(manifest)
    assert all(f"| {name} |" in rendered for name in manifest["tools"])


def test_catalogue_tools_are_classified_by_acceptance_evidence():
    manifest = {"revision": "r", "nx_version": "v2406", "bridge_protocol": 1, "tools": {}}
    rows = [
        {
            "name": "nx_create_blend",
            "domain": "modeling",
            "effect": "model",
            "variants": [],
            "evidence": {
                "v2406": {"variants": [], "scenario": "acceptance/modeling/blend.json", "checks": 9}
            },
        },
        {
            "name": "nx_create_chamfer",
            "domain": "modeling",
            "effect": "model",
            "variants": ["symmetric", "two_offsets"],
            "evidence": {"v2406": {"variants": ["symmetric"]}},
        },
        {
            "name": "nx_create_widget",
            "domain": "modeling",
            "effect": "model",
            "variants": [],
            "evidence": {},
        },
    ]
    rendered = matrix.render(manifest, rows)
    assert "| Native-tested | 2 |" in rendered and "| Experimental | 1 |" in rendered
    assert "9 checks in acceptance/modeling/blend.json" in rendered
    assert "v2406: symmetric (untested: two_offsets)" in rendered
    assert "| nx_create_widget | Experimental |" in rendered


def test_default_matrix_lists_every_catalogue_tool():
    from nx_mcp.core import catalogue

    rows = matrix.load_catalogue()
    assert {r["name"] for r in rows} == set(catalogue.load())
    rendered = matrix.render(matrix.load_manifest(), rows)
    assert "## Catalogue tools" in rendered and "## Legacy tools" in rendered
    assert all(f"| {r['name']} |" in rendered for r in rows)

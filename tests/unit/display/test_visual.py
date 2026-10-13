"""Visual materials and true shading on the shared fake kernel (tests/unit/display/support.py)."""

import pytest

from tests.kit import NXException, run
from tests.unit.display.fakes import STYLES
from tests.unit.display.support import make_library, visual_nx


@pytest.fixture(autouse=True)
def installed(tmp_path, monkeypatch):
    monkeypatch.setenv("UGII_BASE_DIR", str(make_library(tmp_path / "nx")))


def test_list_ranks_library_names_and_counts_part_materials():
    nx = visual_nx()
    block = nx.block(10, 10, 10)
    run(nx, "nx_set_visual_material", objects=[block.ref], material="Teak")
    out = run(nx, "nx_list_visual_materials", query="teak")
    assert out.ok, out.error
    assert [i["name"] for i in out.data["items"]] == ["Teak", "Varnished Teak"]
    assert out.data["items"][0]["category"] == "Woods"
    assert out.data["in_part"] == [{"name": "Teak", "kind": "material", "objects": 1}]
    assert out.data["categories"] == ["Glasses", "Paints", "Plastics", "Woods"]
    by_category = run(nx, "nx_list_visual_materials", query="glasses")
    assert {i["name"] for i in by_category.data["items"]} == {"Clear Glass", "Smoked Glass"}
    paged = run(nx, "nx_list_visual_materials", offset=2, limit=2)
    assert paged.page["total"] == 8 and "categories" not in paged.data
    none = run(nx, "nx_list_visual_materials", query="unobtainium")
    assert none.ok and none.data["items"] == [] and none.warnings[0]["code"] == "NX_NOTE"


def test_set_resolves_a_loose_name_and_reads_back_bodies_and_faces():
    nx = visual_nx()
    block = nx.block(10, 10, 10)
    top = block.faces[0]
    glass = run(nx, "nx_set_visual_material", objects=[block.ref], material="clear  glass")
    assert glass.ok, glass.error
    assert glass.mutation_outcome == "committed"
    assert glass.data["material"] == "Clear Glass" and glass.data["category"] == "Glasses"
    assert glass.data["objects"][0]["material"] == "Clear Glass"
    paint = run(nx, "nx_set_visual_material", objects=[top.ref], material="brush-painted metal RED")
    assert paint.ok and paint.data["objects"][0] == {
        "object": paint.data["objects"][0]["object"],
        "material": "Brush Painted Metal Red",
        "from_body": False,
    }
    info = run(nx, "nx_get_visual_material", objects=[block.ref, block.faces[1].ref])
    rows = info.data["objects"]
    assert [r["material"] for r in rows] == [
        "Clear Glass",
        "Brush Painted Metal Red",
        "Clear Glass",
    ]
    assert rows[1]["object"]["id"] == top.ref and rows[2]["from_body"] is True
    cleared = run(nx, "nx_set_visual_material", objects=[top.ref], remove=True)
    assert cleared.ok and cleared.data["material"] is None
    assert cleared.data["objects"][0]["material"] == "Clear Glass"  # the body's again
    assert cleared.data["objects"][0]["from_body"] is True


def test_set_refuses_unknown_names_and_bad_argument_mixes():
    nx = visual_nx()
    block = nx.block(10, 10, 10)
    unknown = run(nx, "nx_set_visual_material", objects=[block.ref], material="Clear Glas")
    assert unknown.error["code"] == "NX_NOT_FOUND" and unknown.mutation_outcome == "not_started"
    assert "Clear Glass" in unknown.error["details"]["close"]
    both = run(nx, "nx_set_visual_material", objects=[block.ref], material="Teak", remove=True)
    assert both.error["field"] == "/material"
    neither = run(nx, "nx_set_visual_material", objects=[block.ref])
    assert neither.error["code"] == "NX_INVALID_ARGUMENT"


def test_set_without_a_library_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("UGII_BASE_DIR", str(tmp_path / "empty"))
    nx = visual_nx()
    block = nx.block(10, 10, 10)
    out = run(nx, "nx_set_visual_material", objects=[block.ref], material="Teak")
    assert out.error["code"] == "NX_CATALOG_UNAVAILABLE"


def test_a_native_failure_rolls_back():
    nx = visual_nx()
    block = nx.block(10, 10, 10)
    nx.part.VisualMaterialCollection.fail_assign = NXException(3, "boom")
    out = run(nx, "nx_set_visual_material", objects=[block.ref], material="Teak")
    assert out.error["code"] == "NX_BUILD_FAILED" and out.mutation_outcome == "rolled_back"


def test_true_shading_switches_to_studio_reads_back_and_turns_off():
    nx = visual_nx()
    nx.block(10, 10, 10)
    nx.work_view.RenderingStyle = STYLES.Shaded
    on = run(nx, "nx_set_true_shading", material="Chrome", background="white", shadows=False)
    assert on.ok, on.error
    assert on.data["enabled"] is True and on.data["style"] == "studio"
    assert on.data["material"] == "chrome" and on.data["background"] == "white"
    assert on.data["shadows"] is False and on.data["reflections"] is True
    assert on.data["environment"] == "default" and on.data["marks_part_modified"] is True
    assert nx.shading["GlobalMaterialType"] == 15
    keep = run(nx, "nx_set_true_shading", environment="glossy_1")
    assert keep.data["material"] == "chrome" and keep.data["environment"] == "glossy_1"
    off = run(nx, "nx_set_true_shading", enabled=False)
    assert off.ok and off.data["enabled"] is False and off.data["style"] == "shaded"
    assert nx.work_view.RenderingStyle == STYLES.Shaded and off.data["material"] == "chrome"


def test_true_shading_failure_puts_the_view_back():
    nx = visual_nx()
    nx.block(10, 10, 10)
    nx.work_view.RenderingStyle = STYLES.Shaded
    nx.shading_error = NXException(5, "no")
    out = run(nx, "nx_set_true_shading", material="gold")
    assert out.error["code"] == "NX_BUILD_FAILED" and out.mutation_outcome == "rolled_back"
    assert nx.work_view.RenderingStyle == STYLES.Shaded

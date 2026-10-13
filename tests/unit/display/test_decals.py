"""Decal stickers on the shared fake kernel (tests/unit/display/support.py)."""

import math

import pytest

from nx_mcp.domains.display.decals import frame
from tests.kit import NXException, run
from tests.kit.kernel import services
from tests.unit.display.support import make_library, visual_nx


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("UGII_BASE_DIR", str(make_library(tmp_path / "nx")))
    nx = visual_nx()
    block = nx.block(100, 60, 20)
    image = services(nx).root / "images" / "logo.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    return nx, block, image


def test_frame_projects_up_turns_it_and_refuses_up_along_the_normal():
    x, y, z = frame([0, 0, 1])
    assert (x, y, z) == ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0])
    x, y, z = frame([1, 0, 0])  # a side face: the image top is world +Z
    assert y == [0.0, 0.0, 1.0] and x == [0.0, 1.0, 0.0]
    x, y, _ = frame([0, 0, 1], rotation=90)
    assert math.isclose(x[1], 1.0) and math.isclose(y[0], -1.0)
    x, y, _ = frame([0, 0, 1], up=[1, 0, 5])
    assert y == [1.0, 0.0, 0.0]
    with pytest.raises(Exception, match="along the face normal"):
        frame([0, 0, 1], up=[0, 0, 2])


def test_create_places_the_image_and_reads_it_back(setup):
    nx, block, image = setup
    side = block.faces[2]  # +X at x=100
    out = run(nx, "nx_create_decal", face=side.ref, image=str(image), width=40, height=20,
              center=[103, 30, 10], rotation=30, name="HULL_LOGO")  # fmt: skip
    assert out.ok, out.error
    assert out.mutation_outcome == "committed"
    decal = out.data["decal"]
    assert decal["name"] == "HULL_LOGO" and decal["image"].endswith("logo.png")
    assert decal["face"]["id"] == side.ref and decal["center"] == [100.0, 30.0, 10.0]
    assert decal["width"] == 40 and decal["height"] == 20 and decal["projection"] == "planar"
    assert decal["normal"] == [1.0, 0.0, 0.0]
    assert math.isclose(decal["up"][2], math.cos(math.radians(30)))
    assert out.data["projected"] == 3.0 and out.data["marks_part_modified"] is True
    listed = run(nx, "nx_list_visual_materials", limit=1)
    assert {"name": "HULL_LOGO", "kind": "decal", "objects": 1} in listed.data["in_part"]
    again = run(nx, "nx_create_decal", face=side.ref, image=str(image), width=10, name="HULL_LOGO")
    assert again.error["code"] == "NX_NAME_CONFLICT" and again.mutation_outcome == "not_started"


def test_create_keeps_the_aspect_and_defaults_the_center(setup):
    nx, block, image = setup
    nx.image_aspect = 2.0
    out = run(nx, "nx_create_decal", face=block.faces[0].ref, image=str(image), width="30 mm")
    assert out.ok, out.error
    assert out.data["decal"]["height"] == 15.0 and out.data["decal"]["name"] == "Decal Sticker 1"
    assert out.data["decal"]["center"][2] == 20.0


def test_create_refuses_bad_sizes_and_rolls_back_a_native_failure(setup):
    nx, block, image = setup
    bad = run(nx, "nx_create_decal", face=block.faces[0].ref, image=str(image), width=0)
    assert bad.error["field"] == "/width"
    nx.decal_error = NXException(7, "no")
    failed = run(nx, "nx_create_decal", face=block.faces[0].ref, image=str(image), width=10)
    assert failed.error["code"] == "NX_BUILD_FAILED" and failed.mutation_outcome == "rolled_back"


def test_delete_removes_decals_by_name(setup):
    nx, block, image = setup
    run(nx, "nx_create_decal", face=block.faces[0].ref, image=str(image), width=10, name="A")
    run(nx, "nx_set_visual_material", objects=[block.ref], material="Teak")
    gone = run(nx, "nx_delete_decal", names=["A"])
    assert gone.ok and gone.data == {"deleted": ["A"], "count": 1}
    assert "A" not in nx.part.VisualMaterialCollection.items
    missing = run(nx, "nx_delete_decal", names=["Teak"])  # a material, not a decal
    assert missing.error["code"] == "NX_NOT_FOUND" and missing.error["field"] == "/names/0"

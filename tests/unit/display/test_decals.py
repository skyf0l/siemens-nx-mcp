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


def test_delete_takes_a_name_once_in_any_case_and_no_builder_expression_stays(setup):
    """A name given twice deletes its decal once (NX 2406 raised 'Object no longer exists').
    The expressions each sticker builder makes (create, read-back) are deleted again: the
    part ends with the expressions it had (NX 2406 left six per decal)."""
    nx, block, image = setup
    before = list(nx.part.Expressions)
    made = run(nx, "nx_create_decal", face=block.faces[0].ref, image=str(image), width=10,
               name="FRONT_LOGO")  # fmt: skip
    assert made.ok, made.error
    assert list(nx.part.Expressions) == before and made.data["decal"]["width"] == 10
    gone = run(nx, "nx_delete_decal", names=["front logo", "FRONT_LOGO"])
    assert gone.ok, gone.error
    assert gone.data == {"deleted": ["FRONT_LOGO"], "count": 1}
    assert list(nx.part.Expressions) == before


def test_delete_lists_close_names(setup):
    nx, block, image = setup
    run(nx, "nx_create_decal", face=block.faces[0].ref, image=str(image), width=10,
        name="FRONT_LOGO")  # fmt: skip
    missing = run(nx, "nx_delete_decal", names=["FRONT_LOGOS"])
    assert missing.error["code"] == "NX_NOT_FOUND"
    assert missing.error["details"]["close"] == ["FRONT_LOGO"]


def test_create_refuses_a_file_that_is_not_an_image(setup):
    nx, block, image = setup
    bad = image.with_name("not_an_image.png")
    bad.write_bytes(b"hello")
    out = run(nx, "nx_create_decal", face=block.faces[0].ref, image=str(bad), width=10)
    assert out.error["code"] == "NX_UNSUPPORTED_FILE_TYPE" and out.error["field"] == "/image"
    assert out.error["retry"] == "after_fix" and out.mutation_outcome == "not_started"


def test_create_reports_the_image_in_the_workspace_and_a_far_center(setup):
    nx, block, image = setup
    side = block.faces[2]  # +X at x=100
    out = run(nx, "nx_create_decal", face=side.ref, image=str(image), width=20, height=10,
              center=[300, 30, 10])  # fmt: skip
    assert out.ok, out.error
    assert out.data["decal"]["image"] == "images/logo.png"
    assert out.data["projected"] == 200.0
    assert "center lies 200 from the face" in out.warnings[0]["message"]
    near = run(nx, "nx_create_decal", face=side.ref, image=str(image), width=20, height=10,
               center=[100.05, 30, 10], name="NEAR")  # fmt: skip
    assert near.ok and not near.warnings


def test_create_takes_a_width_formula_by_its_value(setup):
    """NX 2406: the decal stores the formula's value; the builder's expression goes again
    and the expression it named stays."""
    nx, block, image = setup
    logo_w = nx.expression("logo_w", "24", 24.0)
    nx.formulas["logo_w/2"] = 12.0
    out = run(nx, "nx_create_decal", face=block.faces[0].ref, image=str(image),
              width="logo_w/2", height=5)  # fmt: skip
    assert out.ok, out.error
    assert out.data["decal"]["width"] == 12.0
    assert list(nx.part.Expressions) == [logo_w]


def test_compensation_deletes_a_placed_decal(setup):
    nx, block, image = setup
    made = run(nx, "nx_create_decal", face=block.faces[0].ref, image=str(image), width=10,
               name="TEMP")  # fmt: skip
    assert made.ok
    undo = run(nx, "nx_undo_operation", mode="compensate")
    assert undo.ok and undo.data["compensated"], undo
    assert "TEMP" not in nx.part.VisualMaterialCollection.items
    assert undo.data["restored"] == ["decal TEMP deleted (the operation placed it)"]


def test_a_curved_projection_says_to_check_the_render(setup):
    nx, block, image = setup
    out = run(nx, "nx_create_decal", face=block.faces[0].ref, image=str(image), width=10,
              projection="cylindrical")  # fmt: skip
    assert out.ok, out.error
    assert any("check it with nx_capture_view" in w["message"] for w in out.warnings)

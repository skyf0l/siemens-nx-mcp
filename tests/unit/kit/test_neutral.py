import pytest

from nx_mcp.kit import neutral
from tests.kit import neutral_files as make

BOXES = [[0, 0, 0, 40, 20, 10], [200, 0, 0, 210, 20, 30]]


@pytest.mark.parametrize("binary", [True, False])
def test_stl_triangles_and_bounds(binary):
    facts = neutral.stl_facts(make.stl(BOXES, binary=binary))
    assert facts == {"binary": binary, "triangles": 24, "min": [0, 0, 0], "max": [210, 20, 30]}


def test_stl_rejects_other_content():
    with pytest.raises(ValueError, match="not an STL"):
        neutral.stl_facts(b"x" * 100)
    truncated = make.stl(BOXES, binary=False)[:-20]
    with pytest.raises(ValueError, match="truncated"):
        neutral.stl_facts(truncated)
    binary = make.stl(BOXES)
    with pytest.raises(ValueError):
        neutral.stl_facts(binary[:-10])  # size no longer matches its count, no 'solid'


@pytest.mark.parametrize("units", ["mm", "in"])
def test_iges_units_entities_and_control_point_bounds(units):
    facts = neutral.iges_facts(make.iges(BOXES, units=units))
    assert facts["units"] == units and facts["version"] == "5.3"
    assert facts["entities"] == {128: 4} and facts["surfaces"] == 4
    assert facts["min"] == [0, 0, 0] and facts["max"] == [210, 20, 30]


def test_iges_transformation_matrix_and_errors():
    data = make.iges([[0, 0, 0, 1, 1, 1]]).decode()
    lines = data.splitlines()
    # Point the first surface at a 124 matrix translating by (100, 0, 0).
    at = next(i for i, line in enumerate(lines) if line[72] == "D")
    lines[at] = lines[at][:48] + f"{5:>8}" + lines[at][56:]
    matrix = "124,1,0,0,100,0,1,0,0,0,0,1,0;"
    d1 = f"{124:>8}{3:>8}{0:>8}{1:>8}{0:>8}{0:>8}{0:>8}{0:>8}{'00000000':>8}"
    d2 = f"{124:>8}{0:>8}{0:>8}{1:>8}{0:>8}{'':>8}{'':>8}{'':>8}{0:>8}"
    d_end = max(i for i, line in enumerate(lines) if line[72] == "D")
    lines.insert(d_end + 1, f"{d1:<72}D{5:>7}")
    lines.insert(d_end + 2, f"{d2:<72}D{6:>7}")
    p_end = max(i for i, line in enumerate(lines) if line[72] == "P")
    lines.insert(p_end + 1, f"{matrix:<64} {5:>7}P{99:>7}")
    facts = neutral.iges_facts("\n".join(lines).encode())
    assert facts["entities"][124] == 1
    assert facts["min"] == [0, 0, 0] and facts["max"] == [101, 1, 1]
    with pytest.raises(ValueError, match="not an IGES"):
        neutral.iges_facts(b"hello")


@pytest.mark.parametrize("binary", [False, True])
def test_parasolid_header(binary):
    facts = neutral.parasolid_facts(make.parasolid(BOXES, binary=binary))
    assert facts == {
        "format": "binary" if binary else "text",
        "schema": "SCH_3601233_36001",
        "units": "m",
    }
    with pytest.raises(ValueError, match="no data"):
        neutral.parasolid_facts(make.parasolid(BOXES).split(b"T51")[0])
    with pytest.raises(ValueError, match="not a Parasolid"):
        neutral.parasolid_facts(b"ISO-10303-21;")


def test_jt_partition_units_and_shapes():
    facts = neutral.jt_facts(make.jt(BOXES))
    assert facts == {
        "version": "10.9",
        "units": "mm",
        "shapes": 2,
        "triangles": 24,
        "min": [0, 0, 0],
        "max": [210, 20, 30],
    }
    assert neutral.jt_facts(make.jt(BOXES, units="inches"))["units"] == "in"
    with pytest.raises(ValueError, match="not a JT"):
        neutral.jt_facts(b"Version x" + bytes(200))
    with pytest.raises(ValueError):
        neutral.jt_facts(make.jt(BOXES)[:150])


@pytest.mark.parametrize(
    ("schema", "units", "expected"),
    [
        ("AUTOMOTIVE_DESIGN { 1 0 10303 214 3 1 1 1 }", "mm", "AP214"),
        ("AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF { 1 0 10303 442 1 1 4 }", "in", "AP242"),
        ("CONFIG_CONTROL_DESIGN", "mm", "AP203"),
    ],
)
def test_step_schema_units_solids(schema, units, expected):
    facts = neutral.step_facts(make.step(2, units=units, schema=schema))
    assert facts == {"schema": expected, "units": units, "solids": 2}


def test_step_rejects_truncated_files():
    with pytest.raises(ValueError, match="truncated"):
        neutral.step_facts(make.step(1)[:-10])
    with pytest.raises(ValueError, match="not a STEP"):
        neutral.step_facts(b"solid x")

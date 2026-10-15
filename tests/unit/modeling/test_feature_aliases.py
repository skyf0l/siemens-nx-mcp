"""nx_get_feature lists every alias nx_edit_feature resolves, and none on locked values."""

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx
from tests.unit.modeling.test_tree import MM, numeric


def block(nx):
    f = feature(nx, "BLOCK")
    rows = []
    for name, text, value in (("p1", "Length (XC)", 40.0), ("p2", "Width (YC)", 20.0),
                              ("p3", "Height (ZC)", 10.0)):  # fmt: skip
        e = nx.expression(name, str(value), value)
        e.Description, e.Units = f"Block(1) {text}", MM
        rows.append(e)
    f.expressions = rows
    return f, rows


def test_block_parameters_list_the_words_edit_feature_takes():
    nx = modeling_nx()
    numeric(nx)
    f, (length, _width, height) = block(nx)
    ref = nx.ref_of(f, "feature")
    data = run(nx, "nx_get_feature", feature=ref).data
    assert [p["aliases"] for p in data["parameters"]] == [["length"], ["width"], ["height"]]
    ok = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "height", "value": 25}])
    assert ok.ok and height.RightHandSide == "25.0", ok
    bad = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "radius", "value": 5}])
    assert bad.error["details"]["aliases"] == {"length": "p1", "width": "p2", "height": "p3"}
    assert length.RightHandSide == "40.0"


def test_locked_parameters_carry_no_alias():
    nx = modeling_nx()
    f = feature(nx, "SYMBOLIC_THREAD")
    pitch = nx.expression("p8", "1.25", 1.25)
    pitch.Description, pitch.Units, pitch.IsNoEdit = "Symbolic Thread(4) Pitch", MM, True
    span = nx.expression("p9", "15", 15.0)
    span.Description, span.Units = "Symbolic Thread(4) Length", MM
    f.expressions = [pitch, span]
    data = run(nx, "nx_get_feature", feature=nx.ref_of(f, "feature")).data
    assert [p["aliases"] for p in data["parameters"]] == [[], ["length"]]

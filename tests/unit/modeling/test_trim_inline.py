"""nx_create_trim_body with an inline plane, and the side of each result body."""

from tests.unit.modeling.fakes import call, feature, modeling_nx, new_body


def test_split_by_an_inline_plane_labels_each_side():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    nx.on_commit("SplitBodyBuilder", lambda b: feature(nx, "SPLIT_BODY", [
        new_body(nx, [10, 0, 0, 40, 20, 10]), new_body(nx, [0, 0, 0, 10, 20, 10])]))  # fmt: skip
    tool = {"origin": [10, 0, 0], "normal": [1, 0, 0]}
    ok = call(nx, "nx_create_trim_body", {"body": block.ref, "tool": tool, "keep": "both"})
    assert ok.ok, ok
    assert ok.data["sides"] == ["front", "back"] and ok.data["volumes"] == [6000.0, 2000.0]
    plane = nx.last("SplitBodyBuilder").BooleanTool.FacePlaneTool.ToolPlane
    assert not plane.SetMethod.calls  # a fixed plane: nothing to stay coincident with


def test_trim_by_a_principal_plane_keeps_back():
    nx = modeling_nx()
    block = nx.block(40, 20, 10, origin=(0, 0, -5))

    def trimmed(b):
        block.box, block.volume, block.centroid = [0, 0, -5, 40, 20, 0], 4000.0, None
        return feature(nx, "TRIM_BODY", [block])

    nx.on_commit("TrimBody2Builder", trimmed)
    ok = call(nx, "nx_create_trim_body", {"body": block.ref, "tool": "xy"})
    assert ok.ok and ok.data["sides"] == ["back"], ok

"""nx_create_text on a path takes the side words left, center and right as anchors."""

from nx_mcp.domains.modeling import text as mtext
from tests.kit import run
from tests.unit.modeling.test_text import nx  # noqa: F401


def test_path_text_takes_center(nx, monkeypatch):  # noqa: F811
    monkeypatch.setattr(mtext.measure, "length", lambda tx, curves: 40.0)
    arc = nx.edge((10, 0, 0), (-10, 0, 0), linear=False)
    ok = run(nx, "nx_create_text", text="AB", path=[arc.ref], height=5, anchor="center")
    assert ok.ok, ok
    b = nx.last("TextBuilder")
    anchors = nx.nx.GeometricUtilities.FrameOnPathBuilder.AnchorLocationType
    assert b.FrameOnPath.AnchorLocation is anchors.Center

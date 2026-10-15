"""Text feature (and its relief extrude) on the shared fake, through the real kernel."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.modeling import text as mtext
from tests.kit import run
from tests.kit.objects import Curve
from tests.unit.modeling.fakes import feature, modeling_nx


def _texts(nx, n=6):
    def hook(_b):
        f = feature(nx, "TEXT", [])
        curves = []
        for i in range(n):
            c = Curve("LINE")
            c.box = [i, 0, 0, i + 1, 7, 0]
            nx.remember(c)
            curves.append(c)
        f.GetEntities = lambda: list(curves)
        return f

    nx.on_commit("TextBuilder", hook)
    nx.part.MeasureManager.NewArcLength = lambda *_a: NS(Length=40.0, Dispose=lambda: None)


@pytest.fixture
def nx():
    fake = modeling_nx()
    _texts(fake)
    return fake


def test_planar_text_frame_font_and_curves(nx, monkeypatch):
    monkeypatch.setattr(mtext.measure, "length", lambda tx, curves: 40.0)
    ok = run(nx, "nx_create_text", text="NX-01", plane="xy", origin=[5, 2, 3], height=7,
             font="Arial", style="bold", anchor="middle_center")  # fmt: skip
    assert ok.ok, ok
    b = nx.last("TextBuilder")
    assert b.SelectFont.calls[0][0][0] == "Arial" and b.TextString == "NX-01"
    assert b.FontStyle is nx.nx.Features.TextBuilder.FontStyleOptions.Bold
    frame = b.PlanarFrame
    anchors = nx.nx.GeometricUtilities.RectangularFrameBuilder.AnchorLocationType
    assert frame.AnchorLocation is anchors.MiddleCenter
    assert frame.Height.RightHandSide == "7.0"
    xform = frame.CoordinateSystem.args[0]  # the fake csys keeps its xform
    origin, x, y = xform.args[0], xform.args[1], xform.args[2]
    assert (origin.X, origin.Y, origin.Z) == (5.0, 2.0, 0.0)  # projected onto the plane
    assert (x.X, x.Y, x.Z) == (1.0, 0.0, 0.0) and (y.X, y.Y, y.Z) == (0.0, 1.0, 0.0)
    assert ok.data["curves"] == 6 and ok.data["max"] == [6.0, 7.0, 0.0]


def test_text_arguments_are_checked_first(nx):
    stale = run(nx, "nx_create_text", text="A", plane="xy", path=["cu:1"])
    assert stale.error["code"] == "NX_STALE_REFERENCE" and stale.error["field"] == "/path/0"
    arc = nx.edge((10, 0, 0), (-10, 0, 0), linear=False)
    both = run(nx, "nx_create_text", text="A", plane="xy", path=[arc.ref])
    assert both.error["category"] == "invalid_argument" and both.mutation_outcome == "not_started"
    blank = run(nx, "nx_create_text", text="  ", plane="xy")
    assert blank.error["field"] == "/text"
    flat = run(nx, "nx_create_text", text="A", plane="xy", x_direction=[0, 0, 1])
    assert flat.error["field"] == "/x_direction"
    block = nx.block(40, 20, 10)
    deep = run(nx, "nx_create_text", text="A", plane="xy",
               relief={"kind": "engrave", "depth": 0, "target": block.ref})  # fmt: skip
    assert deep.error["field"] == "/relief/depth"
    assert not any(b.kind == "TextBuilder" for b in nx.builders)


def test_engrave_extrudes_the_text_against_the_normal(nx, monkeypatch):
    monkeypatch.setattr(mtext.measure, "length", lambda tx, curves: 40.0)
    block = nx.block(40, 20, 10)
    top = block.face((0, 0, 1))

    def cut(_b):
        block.volume = 7900.0
        return feature(nx, "EXTRUDE", [block])

    nx.on_commit("ExtrudeBuilder", cut)
    ok = run(nx, "nx_create_text", text="AB", plane=top.ref, origin=[2, 2, 10], height=5,
             relief={"kind": "engrave", "depth": 1, "target": block.ref})  # fmt: skip
    assert ok.ok, ok
    e = nx.last("ExtrudeBuilder")
    direction = e.Direction
    assert direction is not None
    assert e.Limits.EndExtend.Value.RightHandSide == "1.0"
    assert ok.data["volume"] == 7900.0 and ok.data["relief"]["kind"] == "feature"


def test_text_along_a_curved_edge_anchors_on_it(nx, monkeypatch):
    monkeypatch.setattr(mtext.measure, "length", lambda tx, curves: 40.0)
    arc = nx.edge((10, 0, 0), (-10, 0, 0), linear=False)
    ok = run(nx, "nx_create_text", text="AB", path=[arc.ref], height=5, anchor="bottom_right")
    assert ok.ok, ok
    b = nx.last("TextBuilder")
    assert b.Type is nx.nx.Features.TextBuilder.Types.OnCurve
    where = b.FrameOnPath.AnchorPosition
    assert where.Path.Value is arc and where.Expression.RightHandSide == "100"
    anchors = nx.nx.GeometricUtilities.FrameOnPathBuilder.AnchorLocationType
    assert b.FrameOnPath.AnchorLocation is anchors.Right


def test_a_straight_path_is_refused_before_nx(nx):
    line = nx.edge((0, 0, 0), (40, 0, 0))
    bad = run(nx, "nx_create_text", text="AB", path=[line.ref])
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/path"
    assert bad.mutation_outcome == "not_started" and "x_direction" in bad.error["hint"]

"""Blend corner with setbacks on the shared fake, through the real kernel."""

from types import SimpleNamespace as NS

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx


def _corner(nx, block, faces=9, volume=63207.19):
    """The three edges of ``block`` ending at its (40, 40, 40) vertex, with lengths; commit
    makes a feature owning ``faces`` blend faces and sets the volume."""
    corner = (40.0, 40.0, 40.0)
    edges = []
    for e in block.edges:
        ends = [(v.X, v.Y, v.Z) for v in e.GetVertices()]
        if corner in ends:
            e.GetLength = lambda: 40.0
            edges.append(e)

    def hook(_b):
        block.volume = volume
        f = feature(nx, "BLEND", [block])
        f.own_faces = [nx.face(body=block) for _ in range(faces)]
        return f

    nx.on_commit("EdgeBlendBuilder", hook)
    nx.part.MeasureManager.NewFaceProperties = lambda *_a: NS(Area=910.43, Dispose=lambda: None)
    return edges


def test_blend_corner_adds_setbacks_at_the_shared_vertex():
    nx = modeling_nx()
    block = nx.block(40, 40, 40)
    edges = _corner(nx, block)
    ok = run(nx, "nx_create_blend_corner", edges=[e.ref for e in edges], radius=5, setback=12,
             setback_faces="separate")  # fmt: skip
    assert ok.ok, ok
    b = nx.last("EdgeBlendBuilder")
    (collector, radius), _ = b.AddChainset.calls[0]
    assert collector.GetRules()[0][1] == edges and radius == "5.0"
    (picked, starts, distances), _ = b.AddSetbackData.calls[0]
    assert picked == edges and distances == ["12.0"] * 3
    assert starts == [(e.GetVertices()[0].X, e.GetVertices()[0].Y, e.GetVertices()[0].Z)
                      == (40.0, 40.0, 40.0) for e in edges]  # fmt: skip
    assert "SeparateFromCorner" in str(b.SetbackOption)
    assert ok.data["corner"] == [40.0, 40.0, 40.0] and ok.data["blend_faces"] == 9
    assert ok.data["volume"] == 63207.19 and ok.data["area"] == 910.43


def test_a_plain_vertex_blend_without_patch_rolls_back():
    nx = modeling_nx()
    block = nx.block(40, 40, 40)
    edges = _corner(nx, block, faces=4)
    bad = run(nx, "nx_create_blend_corner", edges=[e.ref for e in edges], radius=5, setback=12)
    assert bad.error["details"]["check"] == "corner_faces"
    assert bad.mutation_outcome == "rolled_back"


def test_blend_corner_arguments_are_checked_first():
    nx = modeling_nx()
    block = nx.block(40, 40, 40)
    edges = _corner(nx, block)
    refs = [e.ref for e in edges]
    small = run(nx, "nx_create_blend_corner", edges=refs, radius=5, setback=5)
    assert small.error["field"] == "/setback" and small.mutation_outcome == "not_started"
    long = run(nx, "nx_create_blend_corner", edges=refs, radius=5, setback=40)
    assert long.error["field"] == "/setback" and long.mutation_outcome == "not_started"
    along_x = [e for e in block.edges if e.GetVertices()[0].X != e.GetVertices()[1].X]
    apart = [e for e in along_x if e not in edges][:3]  # parallel: no shared vertex
    for e in apart:
        e.GetLength = lambda: 40.0
    loose = run(nx, "nx_create_blend_corner", edges=[e.ref for e in apart], radius=5, setback=12)
    assert loose.error["field"] == "/edges" and loose.mutation_outcome == "not_started"
    zero = run(nx, "nx_create_blend_corner", edges=refs, radius=0, setback=12)
    assert zero.error["field"] == "/radius"

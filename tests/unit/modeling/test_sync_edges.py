"""nx_edit_edges (synchronous Move Edge / Offset Edge) on the shared fake through the real
kernel."""

from tests.kit import run
from tests.kit.fake import Factories
from tests.kit.objects import point
from tests.unit.modeling.fakes import feature, modeling_nx


def setup():
    """Block 20x20x10 at x=100 and its top edge along x at y=20."""
    nx = modeling_nx()
    nx.part.Features.SynchronousEdgeCollection = Factories(nx)
    t = nx.block(20, 20, 10, origin=(100.0, 0.0, 0.0))
    top, back = t.face((0, 0, 1)), t.face((0, 1, 0))
    edge = next(
        e
        for e in t.edges
        if [p.Y for p in e.GetVertices()] == [20, 20] and e.GetVertices()[0].Z == 10
    )
    edge.GetFaces = lambda: [back, top]
    return nx, t, edge, top, back


def moves_to(nx, kind, body, edge, shift, volume):
    """Commits of ``kind`` move ``edge`` by ``shift`` (from where it is now)."""

    def hook(b):
        start, end = edge.GetVertices()
        moved = [point(p.X + shift[0], p.Y + shift[1], p.Z + shift[2]) for p in (start, end)]
        edge.GetVertices = lambda: tuple(moved)
        body.volume = volume
        return feature(nx, kind, [body])

    nx.on_commit(kind, hook)


def edit(nx, edge, **args):
    return run(nx, "nx_edit_edges", edges=[nx.ref_of(edge, "edge")], **args)


def test_move_edge_reads_the_displacement_back():
    nx, t, edge, _, _ = setup()
    moves_to(nx, "MoveEdgeBuilder", t, edge, (0, 0, 3), 4600.0)
    ok = edit(nx, edge, action="move", distance=3, direction="z")
    assert ok.ok and ok.data["moved"] == [3.0] and ok.data["volume_change"] == 600.0, ok
    b = nx.last("MoveEdgeBuilder")
    assert b.EdgeToMove.ReplaceRules.calls[0][0][0] == [("EdgeDumb", [edge])] and b.destroyed
    assert b.Motion.DistanceValue.RightHandSide == "3.0"
    moves_to(nx, "MoveEdgeBuilder", t, edge, (0, 0, 2), 4400.0)  # NX fell short
    short = edit(nx, edge, action="move", distance=3, direction="z")
    assert short.error["details"]["check"] == "moved[0]" and short.mutation_outcome == "rolled_back"


def test_offset_takes_the_other_face_when_nx_slid_across_the_wrong_one():
    nx, t, edge, top, back = setup()
    builds = []

    start, end = edge.GetVertices()  # the undo of the first try puts the edge back here

    def hook(b):
        builds.append(b.ReverseOffsetSide)
        dz, dy = (3, 0) if b.ReverseOffsetSide else (0, 3)  # first try: across top
        edge.GetVertices = lambda: tuple(point(p.X, p.Y + dy, p.Z + dz) for p in (start, end))
        t.volume = 4600.0
        return feature(nx, "OFFSET_EDGE", [t])

    nx.on_commit("OffsetEdgeBuilder", hook)
    undone = []
    nx.session.UndoToMark = lambda mark, name: undone.append(mark)
    ok = edit(nx, edge, action="offset", distance=3, along=nx.ref_of(back, "face"))
    assert ok.ok and ok.data["moved"] == [3.0], ok
    assert builds == [False, True] and len(undone) == 1


def test_edge_refusals_before_nx():
    nx, t, edge, top, _ = setup()
    no_dir = edit(nx, edge, action="move", distance=3)
    assert no_dir.error["field"] == "/direction" and no_dir.mutation_outcome == "not_started"
    extra = edit(nx, edge, action="offset", distance=3, direction="z")
    assert extra.error["field"] == "/direction"
    zero = edit(nx, edge, action="offset", distance=0)
    assert zero.error["field"] == "/distance"
    bottom = t.face((0, 0, -1))
    foreign = edit(nx, edge, action="offset", distance=2, along=nx.ref_of(bottom, "face"))
    assert foreign.error["code"] == "NX_WRONG_GEOMETRY" and foreign.error["field"] == "/edges/0"
    along_move = edit(nx, edge, action="move", distance=2, direction="z",
                      along=nx.ref_of(top, "face"))  # fmt: skip
    assert along_move.error["field"] == "/along"
    assert not [b for b in nx.builders if b.kind in ("MoveEdgeBuilder", "OffsetEdgeBuilder")]


def test_an_offset_that_eats_the_face_says_the_distance_is_too_large():
    from tests.kit.objects import NXException

    nx, t, edge, top, _ = setup()
    nx.refuse("OffsetEdgeBuilder", code=630003, message="Operation forbidden on suppressed object")
    bad = edit(nx, edge, action="offset", distance=-200)
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["field"] == "/distance", bad
    assert "smaller distance" in bad.error["hint"] and bad.error["retry"] == "after_fix"
    # NX may commit and leave a dead edge behind: reading it raises the same code.
    nx.errors.clear()

    def eaten(b):
        def dead():
            raise NXException(630003, "Operation forbidden on suppressed object")

        edge.GetVertices = dead
        return feature(nx, "OFFSET_EDGE", [t])

    nx.on_commit("OffsetEdgeBuilder", eaten)
    late = edit(nx, edge, action="offset", distance=-200, along=nx.ref_of(top, "face"))
    assert late.error["code"] == "NX_BUILD_FAILED" and late.error["field"] == "/distance", late
    assert late.mutation_outcome == "rolled_back"

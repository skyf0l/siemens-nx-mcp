"""nx_create_chamfer reports which face took offset (a straight edge between planes)."""

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx


def chamfer_nx(front_gap, top_gap):
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    front, top = block.face((0, -1, 0)), block.face((0, 0, 1))
    edge = nx.remember(nx.edge((0, 0, 10), (40, 0, 10), body=block))
    edge.GetFaces = lambda: [front, top]

    def chamfered(_b):
        block.volume -= 10.0
        lower = nx.edge((0, 0, 10 - front_gap), (40, 0, 10 - front_gap), body=block)
        inner = nx.edge((0, top_gap, 10), (40, top_gap, 10), body=block)
        far = nx.edge((0, 20, 10), (40, 20, 10), body=block)
        front.GetEdges = lambda: [lower, nx.edge((0, 0, 0), (40, 0, 0), body=block)]
        top.GetEdges = lambda: [inner, far]
        return feature(nx, "CHAMFER", [block])

    nx.on_commit("ChamferBuilder", chamfered)
    return nx, edge, front, top


def test_two_offsets_report_the_face_each_offset_runs_along():
    nx, edge, front, top = chamfer_nx(front_gap=3, top_gap=1)
    ok = run(nx, "nx_create_chamfer", edges=[edge.ref], offset=3, offset2=1)
    assert ok.ok, ok
    assert ok.data["offset_face"]["id"] == front.ref and ok.data["offset2_face"]["id"] == top.ref


def test_offset_angle_reports_the_face_of_offset():
    nx, edge, front, top = chamfer_nx(front_gap=3**0.5, top_gap=3)  # 3 @ 30 deg on top
    ok = run(nx, "nx_create_chamfer", edges=[edge.ref], offset=3, angle=30)
    assert ok.ok, ok
    assert ok.data["offset_face"]["id"] == top.ref and ok.data["offset2_face"]["id"] == front.ref


def test_a_symmetric_chamfer_reports_no_faces():
    nx, edge, _front, _top = chamfer_nx(front_gap=2, top_gap=2)
    ok = run(nx, "nx_create_chamfer", edges=[edge.ref], offset=2)
    assert ok.ok and "offset_face" not in ok.data, ok

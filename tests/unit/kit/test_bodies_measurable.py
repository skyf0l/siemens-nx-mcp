"""kit.bodies.measurable: faceless bodies and topology optimization results."""

from types import SimpleNamespace as NS

from nx_mcp.kit import bodies

TopologyOptimizationFeature = type("TopologyOptimizationFeature", (), {})


def body(faces=6, topology=False):
    features = [TopologyOptimizationFeature()] if topology else [NS()]
    return NS(GetFaces=lambda: [NS()] * faces, GetFeatures=lambda: features)


def test_faceless_bodies_are_always_skipped_and_results_only_in_a_scope():
    design, empty, solved = body(), body(0, topology=True), body(4, topology=True)
    assert bodies.measurable([design, empty, solved], scoped=True) == ([design], [empty, solved])
    assert bodies.measurable([design, empty, solved], scoped=False) == (
        [design, solved],
        [empty],
    )


def test_bodies_nx_cannot_describe_are_kept():
    odd = NS()  # no GetFaces / GetFeatures
    assert bodies.measurable([odd], scoped=True) == ([odd], [])

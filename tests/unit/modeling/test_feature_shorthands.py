"""Short argument forms and path/section checks of extrude, revolve, sweep, loft and tube.

Agents first try a plain distance for an extrude limit, a bare principal name for an
axis or direction and one ref for a tube path; those are accepted. A sweep guide or tube
path with sharp corners is refused before NX builds it, and a loft far below its
sections' volume estimate (a twisted loft) warns.
"""

import math
from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.modeling import common
from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body


def extruded(nx, box, volume=None):
    def hook(b):
        return feature(nx, "EXTRUDE", [new_body(nx, box, volume)])

    return hook


def evaluable(nx, paths):
    """Curves evaluable through a minimal ``uf.Eval``: each path maps the normalised
    parameter t to ``(point, unit tangent)``."""
    table = getattr(nx, "paths", None)
    if table is None:
        table = nx.paths = {}
        nx.uf.Eval = NS(
            Initialize=lambda tag: table[int(tag)],
            AskLimits=lambda h: [0.0, 1.0],
            EvaluateUnitVectors=lambda h, t: h(t),
            EvaluateClosestPoint=lambda h, p: (_closest(h, p), None),
            Free=lambda h: None,
        )
    out = []
    for path in paths:
        c = nx.curve()
        c.IsReference = False
        nx.remember(c)
        table[int(c.Tag)] = path
        out.append(c)
    return out


def _closest(path, p, n=720):
    """The parameter of ``path`` nearest ``p`` (sampled)."""
    return min((k / n for k in range(n + 1)), key=lambda t: math.dist(path(t)[0], p))


def seg(a, b):
    d = [b[i] - a[i] for i in range(3)]
    n = math.dist(a, b)
    return lambda t: ([a[i] + t * d[i] for i in range(3)], [v / n for v in d])


def arc(center, r, a0, a1, z=0.0):
    """A counter-clockwise arc in a plane z = const from angle a0 to a1 (radians)."""

    def at(t):
        a = a0 + t * (a1 - a0)
        p = [center[0] + r * math.cos(a), center[1] + r * math.sin(a), z]
        return p, [-math.sin(a), math.cos(a), 0.0]

    return at


def square(half, z=0.0, reverse=False):
    corners = [(-half, -half, z), (half, -half, z), (half, half, z), (-half, half, z)]
    if reverse:
        corners.reverse()
    return [seg(a, b) for a, b in zip(corners, corners[1:] + corners[:1], strict=True)]


# ---- extrude ---------------------------------------------------------------------------


@pytest.mark.parametrize(("end", "text"), [(10, "10.0"), ("10 mm", "10.0"), ("0.5 in", "12.7")])
def test_extrude_takes_a_plain_distance_for_end(end, text):
    nx = modeling_nx()
    sk = nx.sketch()
    height = float(text)
    nx.on_commit("ExtrudeBuilder", extruded(nx, [0, 0, 0, 10, 10, height]))
    ok = run(nx, "nx_create_extrude", sketch=sk.ref, end=end)
    assert ok.ok, ok
    b = nx.last("ExtrudeBuilder")
    assert b.Limits.EndExtend.Value.RightHandSide == text
    assert b.Limits.StartExtend.Value.RightHandSide == "0.0"


def test_extrude_takes_a_principal_direction_name_and_a_plain_start():
    nx = modeling_nx()
    sk = nx.sketch()
    nx.on_commit("ExtrudeBuilder", extruded(nx, [0, 0, -10, 10, 10, -2]))
    ok = run(nx, "nx_create_extrude", sketch=sk.ref, start=2, end="10 mm", direction="-z")
    assert ok.ok, ok
    b = nx.last("ExtrudeBuilder")
    assert b.Limits.StartExtend.Value.RightHandSide == "2.0"
    assert b.Direction.Vector.Z == -1  # the extent read-back ran along -z


def test_extrude_symmetric_plain_distance():
    nx = modeling_nx()
    sk = nx.sketch()
    nx.on_commit("ExtrudeBuilder", extruded(nx, [0, 0, -3, 10, 10, 3]))
    ok = run(nx, "nx_create_extrude", sketch=sk.ref, end=3, symmetric=True)
    assert ok.ok, ok
    assert nx.last("ExtrudeBuilder").Limits.StartExtend.Value.RightHandSide == "-3.0"


@pytest.mark.parametrize(
    ("extra", "field", "words"),
    [
        ({"end": 5, "start": 5}, "/end", "beyond start"),
        ({"end": -2, "symmetric": True}, "/end", "positive"),
        ({"end": "10 deg"}, "/end", "mm or in"),
        ({"end": 5, "direction": "up"}, "/direction", ""),
    ],
)
def test_extrude_plain_distance_errors_name_the_field(extra, field, words):
    nx = modeling_nx()
    sk = nx.sketch()
    bad = run(nx, "nx_create_extrude", sketch=sk.ref, **extra)
    assert bad.error["category"] == "invalid_argument", bad
    assert bad.error["field"] == field and words in bad.error["message"]
    assert bad.mutation_outcome == "not_started" and nx.builders == []


# ---- revolve ---------------------------------------------------------------------------


def test_revolve_takes_a_principal_axis_name():
    nx = modeling_nx()
    sk = nx.sketch()
    nx.on_commit("RevolveBuilder", extruded(nx, [-10, -10, 0, 10, 10, 10]))
    ok = run(nx, "nx_create_revolve", sketch=sk.ref, axis="-y", end_angle="180 deg")
    assert ok.ok, ok
    b = nx.last("RevolveBuilder")
    assert b.Axis.direction.Vector.Y == -1
    assert float(b.Limits.EndExtend.Value.RightHandSide) == pytest.approx(180)


# ---- sweep -----------------------------------------------------------------------------


def test_sweep_refuses_a_guide_with_a_sharp_corner_before_building():
    nx = modeling_nx()
    section, guide = nx.sketch(), nx.sketch()
    guide.geometry = evaluable(nx, [seg((0, 0, 0), (40, 0, 0)), seg((40, 0, 0), (40, 30, 0))])
    bad = run(nx, "nx_create_sweep", section=section.ref, guide=guide.ref)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/guide"
    assert "tangent-continuous" in bad.error["message"] and "fillet" in bad.error["hint"]
    assert bad.error["details"]["corners"] == [{"point": [40.0, 0.0, 0.0], "angle": 90.0}]
    assert bad.mutation_outcome == "not_started" and nx.builders == []


def test_sweep_along_a_line_and_tangent_arc_is_built():
    nx = modeling_nx()
    section, guide = nx.sketch(), nx.sketch()
    # A line along +x ending at (40, 0), then a quarter arc about (40, 10) turning to +y;
    # the arc is drawn backwards (its parameter runs from the far end) on purpose.
    quarter = arc((40, 10), 10, -math.pi / 2, 0.0)
    backwards = lambda t: (quarter(1 - t)[0], [-v for v in quarter(1 - t)[1]])  # noqa: E731
    guide.geometry = evaluable(nx, [seg((0, 0, 0), (40, 0, 0)), backwards])
    nx.on_commit("SweptBuilder", extruded(nx, [0, 0, 0, 50, 10, 1]))
    ok = run(nx, "nx_create_sweep", section=section.ref, guide=guide.ref)
    assert ok.ok, ok


def test_native_tangency_refusal_gets_the_smooth_path_hint():
    nx = modeling_nx()
    section, guide = nx.sketch(), nx.sketch()
    nx.refuse("SweptBuilder", code=670123, message="Some curves in Guide sections not tangent")
    bad = run(nx, "nx_create_sweep", section=section.ref, guide=guide.ref)
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["hint"] == common.SMOOTH_HINT
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0


# ---- loft ------------------------------------------------------------------------------


def loft_nx(volume, reverse=False, top=None):
    nx = modeling_nx()
    a, b = nx.sketch(), nx.sketch(origin=(0, 0, 30))
    a.geometry = evaluable(nx, square(10))
    b.geometry = evaluable(nx, top or square(10, z=30, reverse=reverse))
    nx.on_commit("ThroughCurvesBuilder", extruded(nx, [-10, -10, 0, 10, 10, 30], volume))
    return nx, a, b


def test_a_twisted_loft_warns():
    nx, a, b = loft_nx(4607.0, reverse=True)
    ok = run(nx, "nx_create_loft", sections=[a.ref, b.ref])
    assert ok.ok, ok
    (warning,) = ok.warnings
    assert "twists" in warning["message"] and "12000" in warning["message"]


def test_an_untwisted_loft_and_a_circle_section_do_not_warn():
    nx, a, b = loft_nx(12000.0)
    ok = run(nx, "nx_create_loft", sections=[a.ref, b.ref])
    assert ok.ok and not ok.warnings, ok
    # A circle r 10 on top: estimate (400 + ~314) / 2 * 30; a ~10700 loft is fine.
    nx, a, b = loft_nx(10700.0, top=[arc((0, 0), 10, 0.0, 2 * math.pi, z=30)])
    ok = run(nx, "nx_create_loft", sections=[a.ref, b.ref])
    assert ok.ok and not ok.warnings, ok


def test_loft_estimate_skips_sections_it_cannot_read():
    nx = modeling_nx()
    a, b = nx.sketch(), nx.sketch(origin=(0, 0, 30))  # no evaluable curves
    nx.on_commit("ThroughCurvesBuilder", extruded(nx, [0, 0, 0, 1, 1, 1], 1.0))
    ok = run(nx, "nx_create_loft", sections=[a.ref, b.ref])
    assert ok.ok and not ok.warnings, ok


# ---- tube ------------------------------------------------------------------------------


def test_a_tube_too_large_for_the_self_overlap_check_says_it_was_skipped(monkeypatch):
    from nx_mcp.kit import bodies

    nx = modeling_nx()
    (line,) = evaluable(nx, [seg((0, 0, 0), (50, 0, 0))])
    line.GetLength = lambda: 50.0
    want = math.pi / 4 * 16 * 50
    nx.on_commit(
        "TubeBuilder", lambda b: feature(nx, "TUBE", [new_body(nx, [0, -2, -2, 50, 2, 2], want)])
    )
    monkeypatch.setattr(bodies, "CLASH_FACES", 0)  # every body is "large"
    ok = run(nx, "nx_create_tube", path=line.ref, outer_diameter=4)
    assert ok.ok, ok
    notes = [w["message"] for w in ok.warnings if w["code"] == "NX_NOTE"]
    assert notes and notes[0].startswith("Self-overlap check skipped"), ok.warnings


def test_tube_takes_one_path_ref_and_refuses_corners():
    nx = modeling_nx()
    (line,) = evaluable(nx, [seg((0, 0, 0), (50, 0, 0))])
    line.GetLength = lambda: 50.0
    want = math.pi / 4 * 16 * 50
    nx.on_commit(
        "TubeBuilder", lambda b: feature(nx, "TUBE", [new_body(nx, [0, -2, -2, 50, 2, 2], want)])
    )
    ok = run(nx, "nx_create_tube", path=line.ref, outer_diameter=4)
    assert ok.ok, ok
    assert ok.data["volume"] == pytest.approx(want)
    # Two lines meeting at a right angle: refused before NX, for curves and for a sketch.
    first, second = evaluable(nx, [seg((0, 0, 0), (10, 0, 0)), seg((0, 10, 0), (10, 0, 0))])
    before = len(nx.builders)
    bad = run(nx, "nx_create_tube", path=[first.ref, second.ref], outer_diameter=4)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/path"
    assert bad.error["details"]["corners"][0]["angle"] == pytest.approx(135.0)
    sk = nx.sketch()
    sk.geometry = [first, second]
    bad = run(nx, "nx_create_tube", path=sk.ref, outer_diameter=4)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.mutation_outcome == "not_started"
    assert len(nx.builders) == before


@pytest.mark.parametrize(
    "paths",
    [
        [arc((0, 0), 5, 0.0, 2 * math.pi)],  # a closed curve's seam
        [seg((0, 0, 0), (1, 0, 0)), seg((1, 0, 0), (2, 0, 0)), seg((1, 0, 0), (1, 1, 0))],
        [seg((0, 0, 0), (1, 0, 0)), seg((5, 5, 0), (5, 6, 0))],  # free ends only
    ],
)
def test_corner_check_leaves_seams_branches_and_gaps_to_nx(paths):
    nx = modeling_nx()
    section, guide = nx.sketch(), nx.sketch()
    guide.geometry = evaluable(nx, paths)
    nx.on_commit("SweptBuilder", extruded(nx, [0, 0, 0, 1, 1, 1]))
    ok = run(nx, "nx_create_sweep", section=section.ref, guide=guide.ref)
    assert ok.ok, ok

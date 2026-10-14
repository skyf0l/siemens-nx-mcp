"""Combine read-back per target, NX's own feature alerts, copy volume noise, and the hints of
cheap argument rules (pattern angle, loft alignment), on the shared fake through the kernel."""

from nx_mcp.core.errors import WARNING_CODES
from nx_mcp.domains.modeling import common
from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body

MISSED = "One or more tools did not intersect the target."
# tx.warn keeps a registered code; until NX_TARGET_UNCHANGED is one it reads as an update warning.
UNCHANGED = (
    common.TARGET_UNCHANGED if common.TARGET_UNCHANGED in WARNING_CODES else "NX_UPDATE_WARNING"
)


def _extrude_cut(nx, a, b, cut_b, alerts=()):
    sk = nx.sketch()

    def cut(builder):
        a.volume = 900
        if cut_b:
            b.volume = 900
        f = feature(nx, "EXTRUDE", [a])
        f.GetFeatureWarningMessages = lambda: list(alerts)
        return f

    nx.on_commit("ExtrudeBuilder", cut)
    return run(
        nx,
        "nx_create_extrude",
        sketch=sk.ref,
        end={"type": "distance", "value": 10},
        combine={"operation": "subtract", "targets": [a.ref, b.ref]},
    )


def test_a_target_the_subtract_leaves_unchanged_is_named_in_a_warning():
    nx = modeling_nx()
    a, b = nx.block(10, 10, 10), nx.block(10, 10, 10, origin=(20, 0, 0))
    ok = _extrude_cut(nx, a, b, cut_b=False)
    assert ok.ok and ok.mutation_outcome == "committed", ok
    (warning,) = ok.warnings
    assert warning["code"] == UNCHANGED
    assert f"combine.targets[1] ({b.ref})" in warning["message"]
    assert "did not change it" in warning["message"] and a.ref not in warning["message"]


def test_every_target_cut_gives_no_warning():
    nx = modeling_nx()
    a, b = nx.block(10, 10, 10), nx.block(10, 10, 10, origin=(20, 0, 0))
    ok = _extrude_cut(nx, a, b, cut_b=True)
    assert ok.ok and not ok.warnings, ok


def test_nx_feature_alerts_come_back_as_warnings():
    nx = modeling_nx()
    a, b = nx.block(10, 10, 10), nx.block(10, 10, 10, origin=(20, 0, 0))
    ok = _extrude_cut(nx, a, b, cut_b=True, alerts=["Thin wall left"])
    assert ok.ok, ok
    (warning,) = ok.warnings
    assert warning["code"] == "NX_UPDATE_WARNING"
    assert (
        warning["message"].startswith("NX reports on EXTRUDE(")
        and "Thin wall" in warning["message"]
    )


def test_extrude_that_misses_a_target_entirely_gets_a_hint():
    nx = modeling_nx()
    a, b = nx.block(10, 10, 10), nx.block(10, 10, 10, origin=(20, 0, 0))
    sk = nx.sketch()
    nx.refuse("ExtrudeBuilder", code=common.OUTSIDE_TARGET,
              message="Tool body completely outside target body.")  # fmt: skip
    bad = run(nx, "nx_create_extrude", sketch=sk.ref, end=10,
              combine={"operation": "subtract", "targets": [a.ref, b.ref]})  # fmt: skip
    assert bad.error["nx_code"] == common.OUTSIDE_TARGET, bad
    assert bad.error["field"] == "/combine/targets" and "drop that body" in bad.error["hint"]


def _boolean_hook(nx, target, volume, alerts=()):
    def hook(b):
        for tool in list(b.Tools.Add.calls[0][0][0]):
            nx.part.Bodies.remove(tool)  # NX consumes every tool, a missing one too
        target.volume = volume
        f = feature(nx, "SUBTRACT", [target])
        f.GetFeatureWarningMessages = lambda: list(alerts)
        return f

    nx.on_commit("BooleanBuilder", hook)


def test_boolean_subtract_reports_nx_alert_and_the_tools_that_only_touch():
    nx = modeling_nx()
    target = nx.block(10, 10, 10)
    cutting = nx.block(10, 6, 6, origin=(5, 2, 2))
    touching = nx.block(10, 6, 6, origin=(5, 2, 10))  # sits on the top face
    _boolean_hook(nx, target, 820.0, alerts=[MISSED])
    ok = run(nx, "nx_create_boolean", operation="subtract", target=target.ref,
             tools=[cutting.ref, touching.ref])  # fmt: skip
    assert ok.ok and ok.data["deleted_bodies"] == 2, ok
    (warning,) = ok.warnings
    assert MISSED in warning["message"] and "SUBTRACT(" in warning["message"]
    assert "(tools[1] only touch the target's bounding box)" in warning["message"]


def test_boolean_subtract_that_leaves_the_target_unchanged_warns():
    nx = modeling_nx()
    target = nx.block(10, 10, 10)
    touching = nx.block(10, 6, 6, origin=(5, 2, 10))
    _boolean_hook(nx, target, 1000.0)
    ok = run(nx, "nx_create_boolean", operation="subtract", target=target.ref,
             tools=[touching.ref])  # fmt: skip
    assert ok.ok, ok
    (warning,) = ok.warnings
    assert warning["code"] == UNCHANGED
    assert warning["message"].startswith(f"target ({target.ref}) kept its volume")


# ---------------------------------------------------------------------------
# Copies: NX's mass-property noise stays inside VOLUME_BAND


def test_mirror_scale_and_body_pattern_accept_copy_volume_noise():
    noise = 1 + 6e-5
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    nx.on_commit("MirrorBodyBuilder", lambda b: feature(
        nx, "MIRROR_BODY", [new_body(nx, [-40, 0, 0, 0, 20, 10], 8000 * noise)]))  # fmt: skip
    ok = run(nx, "nx_create_mirror", bodies=[block.ref], plane={"principal": "yz"})
    assert ok.ok, ok

    nx = modeling_nx()
    block = nx.block(10, 10, 10)

    def scaled(b):
        block.box, block.volume = [0, 0, 0, 20, 20, 20], 8000.0 * noise
        return feature(nx, "SCALE", [block])

    nx.on_commit("ScaleBuilder", scaled)
    ok = run(nx, "nx_create_scale", bodies=[block.ref], factor=2)
    assert ok.ok, ok

    nx = modeling_nx()
    blade = nx.block(10, 10, 10)
    nx.on_commit("PatternGeometryBuilder", lambda b: feature(nx, "PATTERN_GEOMETRY", [
        new_body(nx, [20 * i, 0, 0, 20 * i + 10, 10, 10], 1000 * noise) for i in (1, 2)]))  # fmt: skip
    ok = run(nx, "nx_create_pattern", bodies=[blade.ref], layout="linear",
             parameters={"direction": "x", "count": 3, "spacing": 20})  # fmt: skip
    assert ok.ok, ok


# ---------------------------------------------------------------------------
# Hints


def test_negative_circular_angle_is_refused_before_nx_with_the_reverse_axis_hint():
    nx = modeling_nx()
    blade = nx.block(10, 10, 10)
    bad = run(nx, "nx_create_pattern", bodies=[blade.ref], layout="circular",
              parameters={"axis": "z", "count": 4, "angle": -30})  # fmt: skip
    assert bad.mutation_outcome == "not_started" and bad.error["field"] == "/parameters/angle"
    assert "reversing the axis direction" in bad.error["hint"] and "-30" in bad.error["message"]
    assert not nx.builders
    spiral = run(nx, "nx_create_pattern", bodies=[blade.ref], layout="spiral",
                 parameters={"normal": "z", "reference": "x", "angle": -360, "pitch": 5,
                             "spacing": 5})  # fmt: skip
    assert spiral.error["field"] == "/parameters/angle" and "left" in spiral.error["hint"]


def test_loft_alignment_failure_points_at_start_points():
    nx = modeling_nx()
    a, b = nx.sketch(), nx.sketch(origin=(0, 0, 30))
    nx.refuse("ThroughCurvesBuilder", code=670024, message="Unable to align the sections")
    bad = run(nx, "nx_create_loft", sections=[a.ref, b.ref])
    assert bad.error["nx_code"] == 670024, bad
    assert bad.error["field"] == "/start_points" and "start_points" in bad.error["hint"]
    assert bad.mutation_outcome in ("rolled_back", "not_started") and nx.live_builders == 0

"""nx_create_extrude / revolve / sweep / loft on the shared fake, through the real kernel."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body


def extruded(nx, box):
    """Commit hook: the extrude builds one new body with ``box``."""

    def hook(b):
        return feature(nx, "EXTRUDE", [new_body(nx, box)])

    return hook


def test_extrude_new_body_sets_limits_and_checks_the_extent():
    nx = modeling_nx()
    sk = nx.sketch(origin=(0, 0, 0))
    nx.on_commit("ExtrudeBuilder", extruded(nx, [0, 0, 0, 40, 20, 25.4]))
    ok = run(nx, "nx_create_extrude", sketch=sk.ref, end={"type": "distance", "value": "1 in"})
    assert ok.ok, ok
    b = nx.last("ExtrudeBuilder")
    assert b.Limits.StartExtend.Value.RightHandSide == "0.0"
    assert b.Limits.EndExtend.Value.RightHandSide == "25.4"
    assert b.BooleanOperation.Type is nx.nx.GeometricUtilities.BooleanOperation.BooleanType.Create
    assert ok.data["volume"] == pytest.approx(20320) and len(ok.data["bodies"]) == 1
    # A body that ends at z = 10 instead of 12 fails the extent read-back and rolls back.
    nx2 = modeling_nx()
    sk2 = nx2.sketch()
    nx2.on_commit("ExtrudeBuilder", extruded(nx2, [0, 0, 0, 40, 20, 10]))
    bad = run(nx2, "nx_create_extrude", sketch=sk2.ref, end={"type": "distance", "value": 12})
    assert (
        bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.error["details"]["check"] == "extent"
    )
    assert bad.mutation_outcome == "rolled_back" and nx2.live_builders == 0
    assert list(nx2.part.Bodies) == []


def test_symmetric_extrude_spans_both_sides():
    nx = modeling_nx()
    sk = nx.sketch()
    nx.on_commit("ExtrudeBuilder", extruded(nx, [0, 0, -4, 10, 10, 4]))
    ok = run(
        nx, "nx_create_extrude", sketch=sk.ref, end={"type": "distance", "value": 4}, symmetric=True
    )
    assert ok.ok, ok
    b = nx.last("ExtrudeBuilder")
    assert b.Limits.StartExtend.Value.RightHandSide == "-4.0"


def test_subtract_through_all_needs_less_volume():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    sk = nx.sketch(origin=(0, 0, 10))

    def cut(b):
        block.volume = 7000
        return feature(nx, "EXTRUDE", [block])

    nx.on_commit("ExtrudeBuilder", cut)
    ok = run(
        nx,
        "nx_create_extrude",
        sketch=sk.ref,
        end={"type": "through_all"},
        direction={"principal": "-z"},
        combine={"operation": "subtract", "targets": [block.ref]},
    )
    assert ok.ok, ok
    b = nx.last("ExtrudeBuilder")
    types = nx.nx.GeometricUtilities
    assert b.Limits.EndExtend.TrimType is types.Extend.ExtendType.ThroughAll
    assert b.BooleanOperation.Type is types.BooleanOperation.BooleanType.Subtract
    assert b.BooleanOperation.SetTargetBodies.calls[0][0][0] == [block]
    assert ok.data["volume"] == 7000

    # A "subtract" that adds material is not what was asked: rolled back.
    def grow(b):
        block.volume = 9000
        return feature(nx, "EXTRUDE", [block])

    nx.on_commit("ExtrudeBuilder", grow)
    bad = run(
        nx,
        "nx_create_extrude",
        sketch=sk.ref,
        end={"type": "distance", "value": 5},
        combine={"operation": "subtract", "targets": [block.ref]},
    )
    assert bad.error["details"]["check"] == "volume" and bad.mutation_outcome == "rolled_back"


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        (
            {"end": {"type": "distance", "value": 5}, "start": {"type": "distance", "value": 5}},
            "/end/value",
        ),
        ({"end": {"type": "through_all"}}, "/combine"),
        ({"end": {"type": "through_all"}, "symmetric": True}, "/end"),
        ({"end": {"type": "distance", "value": -3}, "symmetric": True}, "/end/value"),
        ({"end": {"type": "distance", "value": "3 deg"}}, "/end"),
    ],
)
def test_extrude_argument_errors_are_not_started(extra, field):
    nx = modeling_nx()
    sk = nx.sketch()
    bad = run(nx, "nx_create_extrude", sketch=sk.ref, **extra)
    assert bad.error["category"] == "invalid_argument", bad
    assert bad.error["field"].startswith(field)
    assert bad.mutation_outcome == "not_started" and nx.builders == []


def test_extrude_until_face_and_native_refusal():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    sk = nx.sketch(origin=(0, 0, 20))
    top = block.face((0, 0, 1))
    nx.refuse("ExtrudeBuilder", code=670030, message="Tool body completely outside target body")
    bad = run(
        nx,
        "nx_create_extrude",
        sketch=sk.ref,
        end={"type": "until_face", "face": top.ref},
        direction=[0, 0, -1],
        combine={"operation": "unite", "targets": [block.ref]},
    )
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 670030
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0
    assert nx.last("ExtrudeBuilder").Limits.EndExtend.Target is top


def test_extrude_refuses_while_a_sketch_is_active():
    nx = modeling_nx()
    sk = nx.sketch()
    nx.session.ActiveSketch = sk
    bad = run(nx, "nx_create_extrude", sketch=sk.ref, end={"type": "distance", "value": 5})
    assert bad.error["code"] == "NX_SKETCH_ACTIVE" and bad.mutation_outcome == "not_started"


def test_revolve_about_a_principal_axis_and_angle_rules():
    nx = modeling_nx()
    sk = nx.sketch(x_axis=(1, 0, 0), y_axis=(0, 0, 1))  # XZ: the z axis lies in it
    nx.on_commit("RevolveBuilder", extruded(nx, [-10, -10, 0, 10, 10, 10]))
    ok = run(
        nx, "nx_create_revolve", sketch=sk.ref, axis={"principal": "z"}, end_angle="3.14159265 rad"
    )
    assert ok.ok, ok
    b = nx.last("RevolveBuilder")
    assert float(b.Limits.EndExtend.Value.RightHandSide) == pytest.approx(180, abs=1e-5)
    assert b.Axis.kind == "axis" and b.Axis.direction.Vector.Z == 1
    bad = run(
        nx, "nx_create_revolve", sketch=sk.ref, axis={"principal": "z"}, start_angle=10, end_angle=5
    )
    assert bad.error["field"] == "/end_angle" and bad.mutation_outcome == "not_started"
    bad = run(nx, "nx_create_revolve", sketch=sk.ref, axis={"principal": "z"}, end_angle=400)
    assert bad.error["field"] == "/end_angle"


def test_revolve_about_a_datum_axis_is_associative():
    nx = modeling_nx()
    sk = nx.sketch()
    axis = nx.datum_axis(origin=(5, 0, 0), direction=(0, 1, 0))
    nx.on_commit("RevolveBuilder", extruded(nx, [0, 0, 0, 10, 10, 10]))
    ok = run(nx, "nx_create_revolve", sketch=sk.ref, axis=axis.ref)
    assert ok.ok, ok
    assert nx.last("RevolveBuilder").Axis.direction.Origin is axis  # direction from the datum


def test_sweep_combines_with_a_boolean_feature():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    section, guide = nx.sketch(), nx.sketch()
    swept = []
    nx.on_commit(
        "SweptBuilder", lambda b: swept.append(extruded(nx, [0, 0, 0, 1, 1, 40])(b)) or swept[-1]
    )

    def united(b):
        block.volume = 1040
        nx.part.Bodies.remove(swept[0].bodies[0])  # NX consumes the tool body
        return feature(nx, "UNITE", [block])

    nx.on_commit("BooleanBuilder", united)
    ok = run(
        nx,
        "nx_create_sweep",
        section=section.ref,
        guide=guide.ref,
        combine={"operation": "unite", "targets": [block.ref]},
    )
    assert ok.ok, ok
    assert ok.data["volume"] == 1040 and ok.data["feature"]["name"] == swept[0].Name
    assert nx.last("BooleanBuilder").Target is block
    same = run(nx, "nx_create_sweep", section=section.ref, guide=section.ref)
    assert same.error["field"] == "/guide" and same.mutation_outcome == "not_started"


def test_loft_sections_in_order_and_body_type():
    nx = modeling_nx()
    a, b2, c = nx.sketch(), nx.sketch(), nx.sketch()
    nx.on_commit("ThroughCurvesBuilder", extruded(nx, [0, 0, 0, 10, 10, 20]))
    ok = run(nx, "nx_create_loft", sections=[a.ref, b2.ref, c.ref])
    assert ok.ok, ok
    assert len(nx.last("ThroughCurvesBuilder").SectionsList.Append.calls) == 3
    dup = run(nx, "nx_create_loft", sections=[a.ref, a.ref])
    assert dup.error["category"] == "invalid_argument" and dup.mutation_outcome == "not_started"
    nx.on_commit("ThroughCurvesBuilder", extruded(nx, [0, 0, 0, 10, 10, 20]))
    sheet = run(nx, "nx_create_loft", sections=[a.ref, b2.ref], solid=False)
    assert sheet.error["details"]["check"] == "body_type"  # NX built a solid


# ---------------------------------------------------------------------------
# Combine read-back: one unite target, a unite that does not touch, several cut targets


def test_unite_takes_one_target_before_building():
    nx = modeling_nx()
    a, b = nx.block(10, 10, 10), nx.block(10, 10, 10, origin=(20, 0, 0))
    sk = nx.sketch()
    bad = run(
        nx,
        "nx_create_extrude",
        sketch=sk.ref,
        end={"type": "distance", "value": 5},
        combine={"operation": "unite", "targets": [a.ref, b.ref]},
    )
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.mutation_outcome == "not_started"
    assert bad.error["field"] == "/combine/targets" and "nx_create_boolean" in bad.error["hint"]
    assert not any(x.kind == "ExtrudeBuilder" for x in nx.builders)
    bad = run(
        nx,
        "nx_create_revolve",
        sketch=sk.ref,
        axis={"principal": "z"},
        combine={"operation": "unite", "targets": [a.ref, b.ref]},
    )
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.mutation_outcome == "not_started"


def test_unite_that_leaves_a_separate_body_fails_and_rolls_back():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    sk = nx.sketch(origin=(0, 0, 20))

    def apart(b):  # NX 2406: the target is unchanged, the tool is a new body
        return feature(nx, "EXTRUDE", [block, new_body(nx, [0, 0, 20, 10, 10, 30])])

    nx.on_commit("ExtrudeBuilder", apart)
    bad = run(
        nx,
        "nx_create_extrude",
        sketch=sk.ref,
        end={"type": "distance", "value": 10},
        combine={"operation": "unite", "targets": [block.ref]},
    )
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.mutation_outcome == "rolled_back"
    assert "does not touch the target" in bad.error["message"]
    assert bad.error["details"]["separate_bodies"] == 1
    assert list(nx.part.Bodies) == [block]

    def merged(b):
        block.volume += 500
        return feature(nx, "EXTRUDE", [block])

    nx.on_commit("ExtrudeBuilder", merged)
    ok = run(
        nx,
        "nx_create_extrude",
        sketch=sk.ref,
        end={"type": "distance", "value": 10},
        combine={"operation": "unite", "targets": [block.ref]},
    )
    assert ok.ok and ok.data["volume"] == 1500 and len(ok.data["bodies"]) == 1, ok


def test_subtract_from_several_targets_reads_every_target():
    nx = modeling_nx()
    a, b = nx.block(10, 10, 10), nx.block(10, 10, 10, origin=(20, 0, 0))
    sk = nx.sketch()

    def cut(builder):  # NX lists one target on the feature; both lost material
        a.volume, b.volume = 900, 900
        return feature(nx, "EXTRUDE", [a])

    nx.on_commit("ExtrudeBuilder", cut)
    ok = run(
        nx,
        "nx_create_extrude",
        sketch=sk.ref,
        end={"type": "distance", "value": 10},
        combine={"operation": "subtract", "targets": [a.ref, b.ref]},
    )
    assert ok.ok, ok
    assert ok.data["volume"] == 1800 and len(ok.data["bodies"]) == 2


def test_revolve_profile_across_the_axis_is_a_clear_argument_error():
    nx = modeling_nx()
    sk = nx.sketch()
    nx.refuse("RevolveBuilder", code=671623, message="Section crosses the axis")
    bad = run(nx, "nx_create_revolve", sketch=sk.ref, axis={"principal": "y"})
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["nx_code"] == 671623
    assert "crosses the revolve axis" in bad.error["message"]
    assert bad.error["field"] == "/axis" and bad.mutation_outcome in ("rolled_back", "not_started")
    assert nx.live_builders == 0


def test_revolve_combine_that_misses_its_targets_says_so():
    """Live: NX 2406 refuses a subtract revolve that misses the target with 671623, the
    code of a profile across the axis; the profile was on one side."""
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    sk = nx.sketch()
    nx.refuse("RevolveBuilder", code=671623, message="Unable to create revolved body")
    bad = run(nx, "nx_create_revolve", sketch=sk.ref, axis={"principal": "x"},
              combine={"operation": "subtract", "targets": [block.ref]})  # fmt: skip
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 671623
    assert bad.error["field"] == "/combine/targets" and "misses" in bad.error["message"]
    assert "crosses" not in bad.error["message"]


def test_revolve_axis_normal_to_or_tilted_out_of_the_sketch_plane_says_so():
    """Live NX 2406: an XY sketch revolved about z is refused with 671623, the code of a
    profile across the axis; the profile was on one side."""
    nx = modeling_nx()
    sk = nx.sketch()  # XY: normal z
    bad = run(nx, "nx_create_revolve", sketch=sk.ref, axis="z")
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.mutation_outcome == "not_started"
    assert "normal to the sketch plane" in bad.error["message"] and bad.error["field"] == "/axis"
    assert not any(b.kind == "RevolveBuilder" for b in nx.builders)
    nx.refuse("RevolveBuilder", code=671623, message="Unable to create revolved body")
    tilted = run(nx, "nx_create_revolve", sketch=sk.ref,
                 axis={"origin": [0, 0, 0], "direction": [1, 0, 1]})  # fmt: skip
    assert tilted.error["code"] == "NX_INVALID_ARGUMENT" and tilted.error["nx_code"] == 671623
    assert "tilted out of the sketch plane" in tilted.error["message"]
    assert "crosses" not in tilted.error["message"]


def _square(nx, corners):
    """Sketch lines around ``corners``, evaluable through a minimal ``uf.Eval``."""
    lines = {}
    nx.uf.Eval = NS(
        Initialize=lambda tag: tag,
        AskLimits=lambda tag: [0.0, 1.0],
        EvaluateUnitVectors=lambda tag, t: (
            [a + t * (b - a) for a, b in zip(*lines[tag], strict=True)],
            [1.0, 0.0, 0.0],
        ),
        Free=lambda tag: None,
    )
    out = []
    for i, (a, b) in enumerate(zip(corners, corners[1:] + corners[:1], strict=True)):
        line = NS(Tag=9000 + i, IsReference=False)
        lines[line.Tag] = (a, b)
        out.append(line)
    return out


def test_revolve_refuses_a_profile_on_both_sides_of_the_axis_before_building():
    nx = modeling_nx()
    sk = nx.sketch()
    sk.geometry = _square(nx, [(-5, 0, 0), (5, 0, 0), (5, 10, 0), (-5, 10, 0)])
    bad = run(nx, "nx_create_revolve", sketch=sk.ref, axis={"principal": "y"})
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.mutation_outcome == "not_started"
    assert "crosses the revolve axis" in bad.error["message"] and bad.error["nx_code"] is None
    assert not any(b.kind == "RevolveBuilder" for b in nx.builders)
    # On one side (touching the axis is fine) the revolve is built.
    sk.geometry = _square(nx, [(0, 0, 0), (5, 0, 0), (5, 10, 0), (0, 10, 0)])
    nx.on_commit("RevolveBuilder", extruded(nx, [-5, 0, -5, 5, 10, 5]))
    ok = run(nx, "nx_create_revolve", sketch=sk.ref, axis={"principal": "y"})
    assert ok.ok, ok


def test_open_profile_extrude_returns_its_sheet():
    nx = modeling_nx()
    sk = nx.sketch(origin=(0, 0, 0))

    def sheet(b):
        body = new_body(nx, [0, 0, 0, 30, 0, 15], volume=0.0)
        body.IsSolidBody, body.IsSheetBody = False, True
        body.GetFaces = lambda: [NS(Tag=1)]
        return feature(nx, "EXTRUDE", [body])

    nx.part.MeasureManager.NewFaceProperties = lambda *a: NS(Area=450.0, Dispose=lambda: None)
    nx.on_commit("ExtrudeBuilder", sheet)
    ok = run(nx, "nx_create_extrude", sketch=sk.ref, end=15)
    assert ok.ok, ok
    assert ok.data["sheet"] is True and ok.data["area"] == 450 and ok.data["volume"] == 0


def test_a_missed_limit_face_says_the_profile_misses_the_target():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    sk = nx.sketch(origin=(200, 0, 10))
    nx.refuse("ExtrudeBuilder", code=671473, message="Limit face not found in the limit direction")
    bad = run(
        nx, "nx_create_extrude", sketch=sk.ref, end={"type": "through_all"},
        combine={"operation": "subtract", "targets": [block.ref]},
    )  # fmt: skip
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 671473
    assert "misses the target body" in bad.error["message"] and "direction" in bad.error["hint"]
    assert bad.mutation_outcome == "rolled_back"


def test_sweep_that_builds_a_self_intersecting_body_rolls_back():
    """Live: section and guide swapped gave ok with a self-intersecting body."""
    nx = modeling_nx()
    section, guide = nx.sketch(), nx.sketch()

    def bad_body(b):
        made = extruded(nx, [0, 0, 0, 1, 1, 40])(b)
        nx.faults[int(made.bodies[0].Tag)] = 1
        return made

    nx.on_commit("SweptBuilder", bad_body)
    bad = run(nx, "nx_create_sweep", section=guide.ref, guide=section.ref)
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.mutation_outcome == "rolled_back"
    assert bad.error["details"]["check"] == "body_health"

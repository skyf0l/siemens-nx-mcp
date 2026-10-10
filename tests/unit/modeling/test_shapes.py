"""nx_create_rib / tube / helix / emboss_body on the shared fake, through the real kernel."""

import math
from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body


def rib_nx():
    nx = modeling_nx()
    nx.part.Features.DesignFeatureCollection = nx.part.Features
    return nx


def test_parallel_rib_unites_and_needs_more_volume():
    nx = rib_nx()
    body = nx.block(40, 20, 40)
    sk = nx.sketch()

    def grow(b):
        body.volume += 900
        return feature(nx, "RIB", [body])

    nx.on_commit("RibBuilder", grow)
    ok = run(
        nx,
        "nx_create_rib",
        sketch=sk.ref,
        body=body.ref,
        thickness="2 mm",
        orientation="parallel",
        side="other_side",
    )
    assert ok.ok, ok
    b = nx.last("RibBuilder")
    types = nx.nx.Features.RibBuilder
    assert b.RibType is types.RibTypes.ParalleltoSectionPlane
    assert b.RibThickness.RightHandSide == "2.0"
    assert b.RibThicknessOffsetOption is types.RibThicknessOffsetOptions.Asymmetric
    assert b.ReverseThicknessDirection is True and b.CombineRibWithTarget is True
    assert b.TargetBody.ReplaceRules.calls and nx.live_builders == 0
    assert ok.data["volume"] == pytest.approx(body.volume)

    # A rib that removes material is not a rib: rolled back.
    nx.on_commit("RibBuilder", lambda b: feature(nx, "RIB", [body]))
    bad = run(nx, "nx_create_rib", sketch=sk.ref, body=body.ref, thickness=2)
    assert bad.error["details"]["check"] == "volume" and bad.mutation_outcome == "rolled_back"


def test_rib_without_combine_returns_the_new_body_and_drafts():
    nx = rib_nx()
    body = nx.block(40, 20, 10)
    sk = nx.sketch(origin=(0, 0, 10))
    nx.on_commit(
        "RibBuilder", lambda b: feature(nx, "RIB", [body, new_body(nx, [19, 2, 2, 21, 18, 10])])
    )
    ok = run(
        nx,
        "nx_create_rib",
        sketch=sk.ref,
        body=body.ref,
        thickness=2,
        draft=3,
        combine=False,
        flip=True,
    )
    assert ok.ok, ok
    b = nx.last("RibBuilder")
    types = nx.nx.Features.RibBuilder
    assert b.RibType is types.RibTypes.PerpendiculartoSectionPlane
    assert b.RibDraftOption is types.RibDraftOptions.FromCap and b.DraftAngle.RightHandSide == "3.0"
    assert b.RibThicknessOffsetOption is types.RibThicknessOffsetOptions.Symmetric
    assert b.ReverseRibDirection is True
    assert ok.data["volume"] == pytest.approx(256) and len(ok.data["bodies"]) == 1


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"thickness": 0}, "/thickness"),
        ({"thickness": 2, "draft": 5, "orientation": "parallel"}, "/draft"),
        ({"thickness": 2, "draft": 95}, "/draft"),
    ],
)
def test_rib_argument_errors_are_not_started(extra, field):
    nx = rib_nx()
    body = nx.block(10, 10, 10)
    sk = nx.sketch()
    bad = run(nx, "nx_create_rib", sketch=sk.ref, body=body.ref, **extra)
    assert bad.error["category"] == "invalid_argument" and bad.error["field"] == field
    assert bad.mutation_outcome == "not_started" and nx.builders == []


def test_native_rib_refusal_keeps_nx_code_and_rolls_back():
    nx = rib_nx()
    body = nx.block(10, 10, 10)
    sk = nx.sketch()
    nx.refuse("RibBuilder", code=670123)
    bad = run(nx, "nx_create_rib", sketch=sk.ref, body=body.ref, thickness=1)
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 670123
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0


def line(nx, length):
    c = nx.curve()
    c.GetLength = lambda: length
    nx.remember(c)
    return c


def tube(nx, outer, inner, length, volume=None):
    want = math.pi / 4 * (outer**2 - inner**2) * length
    nx.on_commit(
        "TubeBuilder",
        lambda b: feature(nx, "TUBE", [new_body(nx, [0, 0, 0, length, 1, 1], volume or want)]),
    )


def test_tube_volume_is_checked_against_pi_r2_length():
    nx = modeling_nx()
    c = line(nx, 50.0)
    tube(nx, 10, 8, 50.0)
    ok = run(nx, "nx_create_tube", path=[c.ref], outer_diameter=10, inner_diameter=8)
    assert ok.ok, ok
    b = nx.last("TubeBuilder")
    assert b.OuterDiameter.RightHandSide == "10.0" and b.InnerDiameter.RightHandSide == "8.0"
    assert b.OutputOption is nx.nx.Features.TubeBuilder.Output.MultipleSegments
    assert ok.data["volume"] == pytest.approx(math.pi * (25 - 16) * 50)
    # NX reporting a different volume fails the read-back.
    tube(nx, 10, 0, 50.0, volume=100.0)
    bad = run(nx, "nx_create_tube", path=[c.ref], outer_diameter=10)
    assert bad.error["details"]["check"] == "volume" and bad.mutation_outcome == "rolled_back"


def test_tube_along_a_sketch_combines():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    sk = nx.sketch()

    def cut(b):
        block.volume = 7000
        return feature(nx, "TUBE", [block])

    nx.on_commit("TubeBuilder", cut)
    ok = run(
        nx,
        "nx_create_tube",
        path=[sk.ref],
        outer_diameter=4,
        combine={"operation": "subtract", "targets": [block.ref]},
    )
    assert ok.ok, ok
    b = nx.last("TubeBuilder")
    assert b.BooleanOption.Type is nx.nx.GeometricUtilities.BooleanOperation.BooleanType.Subtract
    assert ok.data["volume"] == 7000


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"outer_diameter": 0}, "/outer_diameter"),
        ({"outer_diameter": 5, "inner_diameter": 5}, "/inner_diameter"),
        ({"outer_diameter": 5, "inner_diameter": -1}, "/inner_diameter"),
    ],
)
def test_tube_argument_errors(extra, field):
    nx = modeling_nx()
    c = line(nx, 10.0)
    bad = run(nx, "nx_create_tube", path=[c.ref], **extra)
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started"


def test_tube_sketch_path_stands_alone():
    nx = modeling_nx()
    sk = nx.sketch()
    c = line(nx, 10.0)
    bad = run(nx, "nx_create_tube", path=[sk.ref, c.ref], outer_diameter=3)
    assert bad.error["field"] == "/path" and bad.mutation_outcome == "not_started"


# ---- helix ----------------------------------------------------------------------------
def helix_curve(nx, r, p, n, hand="right", origin=(0, 0, 0), length=None):
    """A helix about +z through ``origin`` starting at +x, evaluated analytically."""
    c = nx.curve()
    s = 1.0 if hand == "right" else -1.0
    c.GetLength = lambda: length if length is not None else n * math.hypot(2 * math.pi * r, p)

    def at(t):
        a = 2 * math.pi * n * t
        pt = [origin[0] + r * math.cos(a), origin[1] + s * r * math.sin(a), origin[2] + p * n * t]
        d = [-r * math.sin(a), s * r * math.cos(a), p / (2 * math.pi)]
        k = math.sqrt(sum(v * v for v in d))
        return pt, [v / k for v in d]

    c.at = at
    return c


def helix_nx(curve_of):
    nx = modeling_nx()
    tags = {}

    def commit(b):
        c = curve_of(nx)
        tags[int(c.Tag)] = c
        f = feature(nx, "HELIX")
        f.GetEntities = lambda: [c]
        return f

    nx.on_commit("HelixBuilder", commit)
    nx.uf.Eval = NS(
        Initialize=lambda tag: tags[int(tag)],
        AskLimits=lambda h: (0.0, 1.0),
        EvaluateUnitVectors=lambda h, t: h.at(t),
        EvaluateClosestPoint=lambda h, p: (0.0,),
        Free=lambda h: None,
    )
    return nx


def test_helix_by_turns_reads_back_length_height_and_hand():
    nx = helix_nx(lambda nx: helix_curve(nx, 10, 5, 4))
    ok = run(nx, "nx_create_helix", axis={"principal": "z"}, radius=10, pitch=5, turns=4)
    assert ok.ok, ok
    b = nx.last("HelixBuilder")
    types = nx.nx.Features.HelixBuilder
    assert b.Type is types.Types.AlongVector and b.TurnDirection is types.TurnDirections.RightHand
    assert b.SizeLaw.Value.RightHandSide == "10.0" and b.PitchLaw.Value.RightHandSide == "5.0"
    assert b.Turns.RightHandSide == "4.0" and b.LengthMethod is types.LengthMethods.Turns
    assert ok.data["length"] == pytest.approx(4 * math.hypot(20 * math.pi, 5))
    assert ok.data["end"] == pytest.approx([10, 0, 20])


def test_helix_by_height_and_left_hand():
    nx = helix_nx(lambda nx: helix_curve(nx, 2, 1.5, 4, hand="left"))
    ok = run(
        nx, "nx_create_helix", axis={"principal": "z"}, radius=2, pitch=1.5, height=6, hand="left"
    )
    assert ok.ok, ok
    assert ok.data["turns"] == pytest.approx(4)
    assert (
        nx.last("HelixBuilder").TurnDirection is nx.nx.Features.HelixBuilder.TurnDirections.LeftHand
    )


@pytest.mark.parametrize(
    ("curve_of", "check"),
    [
        (lambda nx: helix_curve(nx, 10, 5, 4, length=200.0), "length"),
        (lambda nx: helix_curve(nx, 10, 5, 4, hand="left"), "hand"),
    ],
)
def test_helix_readback_failures_roll_back(curve_of, check):
    nx = helix_nx(curve_of)
    bad = run(nx, "nx_create_helix", axis={"principal": "z"}, radius=10, pitch=5, turns=4)
    assert bad.error["details"]["check"] == check and bad.mutation_outcome == "rolled_back"


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"radius": 1, "pitch": 1}, None),
        ({"radius": 1, "pitch": 1, "turns": 2, "height": 3}, None),
        ({"radius": 0, "pitch": 1, "turns": 2}, "/radius"),
        ({"radius": 1, "pitch": -1, "turns": 2}, "/pitch"),
    ],
)
def test_helix_argument_errors(extra, field):
    nx = helix_nx(lambda nx: helix_curve(nx, 1, 1, 1))
    bad = run(nx, "nx_create_helix", axis={"principal": "z"}, **extra)
    assert bad.error["category"] == "invalid_argument" and bad.mutation_outcome == "not_started"
    if field:
        assert bad.error["field"] == field


# ---- emboss body ----------------------------------------------------------------------
def test_emboss_body_sets_clearance_and_needs_a_volume_change():
    nx = modeling_nx()
    target = nx.block(40, 20, 10)
    tool = nx.block(5, 5, 5)

    def form(b):
        target.volume += 120
        return feature(nx, "EMBOSS_BODY", [target])

    nx.on_commit("EmbossBodyBuilder", form)
    ok = run(nx, "nx_create_emboss_body", target=target.ref, tool_body=tool.ref, clearance=0.5)
    assert ok.ok, ok
    b = nx.last("EmbossBodyBuilder")
    assert b.Clearance.RightHandSide == "0.5" and b.KeepTools is True
    assert b.TargetBody.GetRules() == [("BodyDumb", [target])]
    assert ok.data["volume"] == pytest.approx(target.volume) and nx.live_builders == 0
    # An emboss that leaves the target unchanged did nothing: rolled back.
    nx.on_commit("EmbossBodyBuilder", lambda b: feature(nx, "EMBOSS_BODY", [target]))
    bad = run(nx, "nx_create_emboss_body", target=target.ref, tool_body=tool.ref)
    assert bad.error["details"]["check"] == "volume" and bad.mutation_outcome == "rolled_back"


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"clearance": -1}, "/clearance"),
        ({"thickness": 0}, "/thickness"),
        ({"same": True}, "/tool_body"),
    ],
)
def test_emboss_body_argument_errors(extra, field):
    nx = modeling_nx()
    target = nx.block(10, 10, 10)
    tool = nx.block(2, 2, 2)
    if extra.pop("same", False):
        tool = target
    bad = run(nx, "nx_create_emboss_body", target=target.ref, tool_body=tool.ref, **extra)
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started"


def test_inch_tube_length_comes_from_millimetres():
    nx = modeling_nx(units="in")
    c = line(nx, 50.8)  # NX answers GetLength in mm
    tube(nx, 0.5, 0.25, 2.0, volume=math.pi / 4 * (0.25 - 0.0625) * 2 * 25.4**3)  # mm^3
    ok = run(nx, "nx_create_tube", path=[c.ref], outer_diameter=0.5, inner_diameter="6.35 mm")
    assert ok.ok, ok
    assert nx.last("TubeBuilder").InnerDiameter.RightHandSide == "0.25"

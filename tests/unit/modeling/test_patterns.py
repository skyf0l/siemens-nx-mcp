"""Mirror (bodies, features) and pattern layouts on the shared fake, through the real kernel."""

import pytest

from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body


def counted(f, count):
    """A committed pattern feature reporting ``count`` instances, the seed included."""
    f.instances = [object() for _ in range(count - 1)]
    f.GetImmediateContainedFeatures = lambda: list(f.instances)
    return f


# ---------------------------------------------------------------------------
# Mirror


def test_mirror_bodies_checks_the_reflection_and_hides_sources():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)

    def mirrored(b):
        return feature(nx, "MIRROR_BODY", [new_body(nx, [-40, 0, 0, 0, 20, 10])])

    nx.on_commit("MirrorBodyBuilder", mirrored)
    ok = run(nx, "nx_create_mirror", target="bodies", bodies=[block.ref],
             plane={"principal": "yz"}, keep_original=False)  # fmt: skip
    assert ok.ok, ok
    assert block.IsBlanked and ok.data["volume"] == 8000 and ok.data["count"] == 1
    nx.on_commit("MirrorBodyBuilder", lambda b: feature(nx, "MIRROR_BODY",
                 [new_body(nx, [0, 0, 0, 40, 20, 10])]))  # fmt: skip
    bad = run(nx, "nx_create_mirror", bodies=[block.ref],
              plane={"origin": [0, 0, 0], "normal": [1, 0, 0]})  # fmt: skip
    assert bad.error["details"]["check"] == "mirror[0]" and bad.mutation_outcome == "rolled_back"


def test_mirror_features_uses_the_mirror_layout_and_reads_the_volume():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    seed = feature(nx, "EXTRUDE", [block])
    plane = nx.datum_plane((20, 0, 0), (1, 0, 0))

    def mirrored(b):
        block.volume += 30
        return feature(nx, "MIRROR", [block])

    nx.on_commit("MirrorBuilder", mirrored)
    ok = run(nx, "nx_create_mirror", target="features", features=[nx.ref_of(seed, "feature")],
             plane=nx.ref_of(plane, "datum_plane"))  # fmt: skip
    assert ok.ok, ok
    assert ok.data["target"] == "features" and ok.data["count"] == 1
    b = nx.last("MirrorBuilder")
    assert b.PatternService.MirrorDefinition.ExistingPlane.Value is plane
    assert b.FeatureList.Add.calls[0][0][0] == [seed] and b.destroyed
    nx.on_commit("MirrorBuilder", lambda b: feature(nx, "MIRROR", [block]))
    same = run(nx, "nx_create_mirror", target="features", features=[nx.ref_of(seed, "feature")],
               plane={"principal": "yz"})  # fmt: skip
    assert same.error["details"]["check"] == "volume" and same.mutation_outcome == "rolled_back"


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"target": "features"}, "/features"),
        ({"target": "bodies"}, "/bodies"),
        ({"target": "bodies", "bodies": ["B"], "features": ["F"]}, "/features"),
        ({"target": "features", "features": ["F"], "keep_original": False}, "/keep_original"),
    ],
)
def test_mirror_target_errors(extra, field):
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    seed = feature(nx, "EXTRUDE", [block])
    refs = {"B": block.ref, "F": nx.ref_of(seed, "feature")}
    for key in ("bodies", "features"):
        if key in extra:
            extra[key] = [refs[r] for r in extra[key]]
    bad = run(nx, "nx_create_mirror", plane={"principal": "yz"}, **extra)
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    assert not any(b.commits for b in nx.builders)


# ---------------------------------------------------------------------------
# Pattern


def pattern_nx(count=None):
    """A fake whose pattern commit removes 50 per instance and reports ``count`` instances
    (default: the requested linear count)."""
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    seed = feature(nx, "HOLE PACKAGE", [block])
    nx.count = count

    def commit(b):
        made = nx.count
        if made is None:
            made = int(b.PatternService.RectangularDefinition.XSpacing.NCopies.RightHandSide)
        block.volume -= 50 * (made - 1)
        return counted(feature(nx, "PATTERN_FEATURE", [block]), made + nx.miscount)

    nx.miscount = 0
    nx.on_commit("PatternFeatureBuilder", commit)
    return nx, block, seed


def pattern(nx, seed, layout, **parameters):
    return run(nx, "nx_create_pattern", features=[nx.ref_of(seed, "feature")], layout=layout,
               parameters=parameters)  # fmt: skip


def test_linear_pattern_reads_back_the_instances():
    nx, block, seed = pattern_nx()
    ok = pattern(nx, seed, "linear", direction=[1, 0, 0], count=3, spacing="0.5 in")
    assert ok.ok and ok.data["count"] == 3 and ok.data["layout"] == "linear", ok
    b = nx.last("PatternFeatureBuilder")
    d = b.PatternService.RectangularDefinition
    assert d.XSpacing.PitchDistance.RightHandSide == repr(12.7)
    assert d.YSpacing.NCopies.RightHandSide == "1" and b.destroyed
    nx.miscount = 1
    bad = pattern(nx, seed, "linear", direction=[1, 0, 0], count=3, spacing=5)
    assert bad.error["details"]["check"] == "count" and nx.live_builders == 0


def test_linear_direction_from_an_edge_stays_associative():
    nx, block, seed = pattern_nx(count=4)
    edge = nx.edge((0, 0, 0), (0, 20, 0), body=block)
    ok = pattern(nx, seed, "linear", direction=nx.ref_of(edge, "edge"), count=2, spacing=8,
                 direction2=[1, 0, 0], count2=2, spacing2=10)  # fmt: skip
    assert ok.ok and ok.data["count"] == 4, ok
    d = nx.last("PatternFeatureBuilder").PatternService.RectangularDefinition
    assert d.XDirection.Origin is edge and d.UseYDirectionToggle is True
    assert d.YDirection.Vector.X == 1.0


def test_circular_polygon_spiral_and_general_configure_their_definitions():
    nx, block, seed = pattern_nx(count=6)
    ok = pattern(nx, seed, "circular", axis={"principal": "z"}, count=6, angle=60)
    assert ok.ok, ok
    c = nx.last("PatternFeatureBuilder").PatternService.CircularDefinition
    assert c.AngularSpacing.PitchAngle.RightHandSide == repr(60.0)

    nx.count = 12
    ok = pattern(nx, seed, "polygon", axis={"principal": "z"}, sides=4, count_per_side=4,
                 size="circumscribed")  # fmt: skip
    assert ok.ok and ok.data["count"] == 12, ok
    p = nx.last("PatternFeatureBuilder").PatternService.PolygonDefinition
    assert p.NumberOfSides.RightHandSide == "4" and p.PolygonSpacing.NCopies.RightHandSide == "4"
    assert "Circumscribed" in repr(p.PolygonSizeOption)

    nx.count = 9
    ok = pattern(nx, seed, "spiral", normal={"principal": "z"}, reference=[1, 0, 0], angle=720,
                 pitch=10, spacing=12, hand="left")  # fmt: skip
    assert ok.ok and ok.data["count"] == 9, ok
    s = nx.last("PatternFeatureBuilder").PatternService.SpiralDefinition
    assert s.TotalAngle.RightHandSide == repr(720.0) and "Lefthand" in repr(s.DirectionType)
    assert s.PitchAlongSpiral.OnPathPitchDistance.Expression.RightHandSide == repr(12.0)
    assert s.PitchAlongSpiral.OnPathPitchDistance.IsPercentUsed is False
    tilted = pattern(nx, seed, "spiral", normal=[0, 0, 1], reference=[1, 0, 1], angle=360,
                     pitch=5, spacing=5)  # fmt: skip
    assert tilted.error["field"] == "/parameters/reference", tilted

    nx.count = 3
    ok = pattern(nx, seed, "general", origin=[5, 5, 10], positions=[[15, 5, 10], [25, 5, 10]])
    assert ok.ok and ok.data["count"] == 3, ok
    g = nx.last("PatternFeatureBuilder").PatternService.GeneralDefinition
    assert g.ToPoints.AddToSection.calls and g.FromLocationPoint.kind == "point"


def test_along_curve_and_fill():
    nx, block, seed = pattern_nx(count=3)
    edge = nx.edge((0, 0, 10), (40, 0, 10), body=block)
    edge.GetLength = lambda: 40.0
    ok = pattern(nx, seed, "along_curve", curve=nx.ref_of(edge, "edge"), count=3, spacing=15)
    assert ok.ok, ok
    a = nx.last("PatternFeatureBuilder").PatternService.AlongPathDefinition
    pitch = a.XOnPathSpacing.OnPathPitchDistance
    assert pitch.Expression.RightHandSide == repr(15.0) and pitch.IsPercentUsed is False
    assert a.XPathOption is not None and pitch.Update.calls
    far = pattern(nx, seed, "along_curve", curve=nx.ref_of(edge, "edge"), count=4, spacing=15)
    assert far.error["field"] == "/parameters/spacing" and far.mutation_outcome == "not_started"

    nx.count = 8
    top = nx.face((0, 0, 1), (0, 0, 10), body=block)
    top.GetEdges = lambda: []
    top.box = [0, 0, 10, 30, 40, 10]
    nx.remember(top)
    nx.part.ScRuleFactory.CreateRuleOuterEdgesOfFaces = lambda faces: ("Outer", list(faces))
    ok = pattern(nx, seed, "fill", boundary=nx.ref_of(top, "face"), direction=[1, 0, 0],
                 spacing=10, spacing2=25, margin=1)  # fmt: skip
    assert ok.ok and ok.data["count"] == 8, ok
    service = nx.last("PatternFeatureBuilder").PatternService
    d = service.RectangularDefinition
    assert service.PatternFill.FillBoundary.AddToSection.calls[0][0][1] is top
    assert d.XSpacing.NCopies.RightHandSide == "6" and d.YSpacing.NCopies.RightHandSide == "3"
    assert d.XSymmetryToggle is True and d.YDirection.Vector.Y == 1.0
    skew = pattern(nx, seed, "fill", boundary=nx.ref_of(top, "face"), direction=[0, 0, 1],
                   spacing=10)  # fmt: skip
    assert skew.error["field"] == "/parameters/direction", skew
    nx.count = 1
    none = pattern(nx, seed, "fill", boundary=nx.ref_of(top, "face"), direction=[1, 0, 0],
                   spacing=10)  # fmt: skip
    assert none.error["details"]["check"] == "count" and none.mutation_outcome == "rolled_back"


@pytest.mark.parametrize(
    ("layout", "parameters", "field"),
    [
        ("circular", {"axis": {"principal": "z"}, "count": 7, "angle": 60}, "/parameters/angle"),
        ("linear", {"direction": [1, 0, 0], "count": 2, "spacing": 5, "count2": 2}, "/parameters"),
        (
            "linear",
            {
                "direction": [1, 0, 0],
                "count": 2,
                "spacing": 5,
                "direction2": [2, 0, 0],
                "count2": 2,
                "spacing2": 3,
            },
            "/parameters/direction2",
        ),  # fmt: skip
        ("linear", {"direction": [1, 0, 0], "count": 2, "spacing": 0}, "/parameters/spacing"),
        (
            "linear",
            {
                "direction": [1, 0, 0],
                "count": 50,
                "spacing": 1,
                "direction2": [0, 1, 0],
                "count2": 50,
                "spacing2": 1,
            },
            "/parameters",
        ),  # fmt: skip
        (
            "spiral",
            {"normal": [0, 0, 1], "reference": [1, 0, 0], "angle": 360, "pitch": -1, "spacing": 5},
            "/parameters/pitch",
        ),  # fmt: skip
        (
            "general",
            {"origin": [0, 0, 0], "positions": [[1, 0, 0], [1, 0, 0]]},
            "/parameters/positions/1",
        ),  # fmt: skip
        ("general", {"origin": [0, 0, 0], "positions": [[0, 0, 0]]}, "/parameters/positions/0"),
        (
            "fill",
            {"boundary": "fc:1", "direction": [1, 0, 0], "spacing": 2, "margin": -1},
            "/parameters/margin",
        ),  # fmt: skip
    ],
)
def test_pattern_layout_errors(layout, parameters, field):
    nx, block, seed = pattern_nx()
    bad = pattern(nx, seed, layout, **parameters)
    assert bad.error["category"] == "invalid_argument" or bad.error["code"].startswith("NX_TOO"), (
        bad
    )
    assert bad.error["field"] == field, bad
    assert nx.live_builders == 0 and not any(b.commits for b in nx.builders)


def test_pattern_parameters_follow_the_layout_form():
    nx, block, seed = pattern_nx()
    bad = pattern(nx, seed, "polygon", axis={"principal": "z"}, sides=2, count_per_side=3)
    assert bad.error["category"] == "invalid_argument" and "sides" in bad.error["field"], bad
    extra = pattern(nx, seed, "circular", axis={"principal": "z"}, count=3, angle=10, spacing=4)
    assert extra.error["category"] == "invalid_argument", extra

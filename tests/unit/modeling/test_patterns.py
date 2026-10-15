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
    assert ok.data["new_bodies"] == 1
    nx.on_commit("MirrorBodyBuilder", lambda b: feature(nx, "MIRROR_BODY",
                 [new_body(nx, [0, 0, 0, 40, 20, 10])]))  # fmt: skip
    bad = run(nx, "nx_create_mirror", bodies=[block.ref],
              plane={"origin": [0, 0, 0], "normal": [1, 0, 0]})  # fmt: skip
    assert bad.error["details"]["check"] == "mirror[0]" and bad.mutation_outcome == "rolled_back"


def test_mirror_bodies_reports_the_hidden_sources():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    nx.on_commit("MirrorBodyBuilder", lambda b: feature(nx, "MIRROR_BODY",
                 [new_body(nx, [-40, 0, 0, 0, 20, 10])]))  # fmt: skip
    ok = run(nx, "nx_create_mirror", bodies=[block.ref], plane="yz", keep_original=False)
    assert ok.ok, ok
    assert [r["id"] for r in ok.data["hidden"]] == [block.ref]
    kept = run(nx, "nx_create_mirror", bodies=[block.ref], plane="yz")
    assert kept.ok and "hidden" not in kept.data


def test_mirror_infers_target_features_and_takes_a_datum_plane_feature():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    seed = feature(nx, "EXTRUDE", [block])
    datum = feature(nx, "DATUM_PLANE", [])
    datum.DatumPlane = nx.datum_plane((20, 0, 0), (1, 0, 0))

    def mirrored(b):
        block.volume += 30
        return feature(nx, "MIRROR", [block])

    nx.on_commit("MirrorBuilder", mirrored)
    ok = run(nx, "nx_create_mirror", features=[nx.ref_of(seed, "feature")],
             plane=nx.ref_of(datum, "feature"))  # fmt: skip
    assert ok.ok, ok
    assert ok.data["target"] == "features"
    b = nx.last("MirrorBuilder")
    assert b.PatternService.MirrorDefinition.ExistingPlane.Value is datum.DatumPlane
    plain = feature(nx, "BLOCK", [])
    bad = run(nx, "nx_create_mirror", features=[nx.ref_of(seed, "feature")],
              plane=nx.ref_of(plain, "feature"))  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_KIND" and bad.mutation_outcome == "not_started"
    assert "datum plane" in bad.error["hint"]


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
    assert ok.data["new_bodies"] == 0
    b = nx.last("MirrorBuilder")
    assert b.PatternService.MirrorDefinition.ExistingPlane.Value is plane
    assert b.FeatureList.Add.calls[0][0][0] == [seed] and b.destroyed
    nx.on_commit("MirrorBuilder", lambda b: feature(nx, "MIRROR", [block]))
    same = run(nx, "nx_create_mirror", target="features", features=[nx.ref_of(seed, "feature")],
               plane={"principal": "yz"})  # fmt: skip
    assert same.error["details"]["check"] == "volume" and same.mutation_outcome == "rolled_back"


def test_mirror_of_a_feature_with_its_own_body_counts_the_new_body():
    # NX 2406: the mirror of a seed that made its own body is a new body; the seed body
    # keeps its volume (it failed the volume check before new bodies were counted in).
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    seed = feature(nx, "EXTRUDE", [block])
    nx.on_commit("MirrorBuilder", lambda b: feature(nx, "MIRROR", [
        new_body(nx, [-10, 0, 0, 0, 10, 10])]))  # fmt: skip
    ok = run(nx, "nx_create_mirror", target="features", features=[nx.ref_of(seed, "feature")],
             plane={"principal": "yz"})  # fmt: skip
    assert ok.ok, ok
    assert ok.data["new_bodies"] == 1 and ok.data["count"] == 1
    assert ok.data["volume"] == 2000 and len(ok.data["bodies"]) == 2


def test_unite_into_is_read_back_like_a_boolean_unite():
    """The same checks as nx_create_boolean: more than target plus copies, or a copy left
    out (its inner point outside the result), rolls back; a 1e-5 drift is accepted."""
    nx = modeling_nx()
    disk = nx.block(100, 100, 5, origin=(-50, -50, -5))
    blade = nx.block(10, 10, 10)
    _body_copies(nx, blade, 2)

    def unite(gain, lost=()):
        def united(b):
            for body in b.Tools.Add.calls[0][0][0]:
                nx.part.Bodies.remove(body)
                disk.volume += 0.0 if body in lost else body.volume
            disk.volume *= gain
            return feature(nx, "UNITE", [disk])

        nx.on_commit("BooleanBuilder", united)
        return run(nx, "nx_create_pattern", bodies=[blade.ref], layout="linear",
                   parameters={"direction": "x", "count": 3, "spacing": 20},
                   unite_into=disk.ref)  # fmt: skip

    grown = unite(1.5)
    assert grown.error["code"] == "NX_VERIFICATION_FAILED", grown
    assert grown.error["details"]["check"] == "volume" and grown.mutation_outcome == "rolled_back"
    nx.uf.Modeling.AskFaceProps = lambda tag, uv: ([5, 5, 10], 0, 0, 0, 0, [0, 0, 1], 0, 0)
    blade.contains = lambda p: 1  # the seed's inner point
    disk.contains = lambda p: 2  # ... lies outside the result
    dropped = unite(1.0, lost=(blade,))
    assert dropped.error["code"] == "NX_VERIFICATION_FAILED", dropped
    assert dropped.error["details"]["check"] == "tools" and dropped.error["details"]["dropped"] == [
        0
    ]
    assert "patterned body 0" in dropped.error["message"] and dropped.error.get("field") is None
    drift = unite(1 + 1e-5)  # B-spline mass properties: within the 1e-4 band
    assert drift.ok, drift


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


def test_mirror_plane_errors_are_not_started():
    """Live: a zero plane normal was reported rolled_back."""
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    bad = run(nx, "nx_create_mirror", bodies=[block.ref],
              plane={"origin": [0, 0, 0], "normal": [0, 0, 0]})  # fmt: skip
    assert bad.error["field"] == "/plane/normal" and bad.mutation_outcome == "not_started"


# ---------------------------------------------------------------------------
# Pattern


def pattern_nx(count=None, units="mm"):
    """A fake whose pattern commit removes 50 per instance and reports ``count`` instances
    (default: the requested linear count)."""
    nx = modeling_nx(units=units)
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


def test_along_curve_compares_the_spacing_with_the_length_in_part_units():
    nx, block, seed = pattern_nx(count=3, units="in")
    edge = nx.edge((0, 0, 10), (2, 0, 10), body=block)
    edge.GetLength = lambda: 50.8  # NX 2406 answers millimetres: 2 in
    far = pattern(nx, seed, "along_curve", curve=nx.ref_of(edge, "edge"), count=3, spacing=1.5)
    assert far.error["code"] == "NX_INVALID_ARGUMENT", far
    assert far.error["field"] == "/parameters/spacing" and far.mutation_outcome == "not_started"
    ok = pattern(nx, seed, "along_curve", curve=nx.ref_of(edge, "edge"), count=3, spacing=1)
    assert ok.ok, ok


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
                "count": 2,
                "spacing": -10,
                "direction2": [0, 1, 0],
                "count2": 2,
                "spacing2": 0,
            },
            "/parameters/spacing2",
        ),  # fmt: skip  (live: the zero spacing2 was reported on /spacing)
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


def test_general_pattern_listing_the_seed_says_to_list_only_the_copies():
    # Live: agents listed the seed's own place among the positions.
    nx, block, seed = pattern_nx()
    bad = pattern(nx, seed, "general", origin=[5, 5, 10], positions=[[5, 5, 10], [15, 5, 10]])
    assert bad.error["code"] == "NX_INVALID_ARGUMENT", bad
    assert bad.error["field"] == "/parameters/positions/0"
    assert "only the copies" in bad.error["hint"]


def test_pattern_parameters_follow_the_layout_form():
    nx, block, seed = pattern_nx()
    bad = pattern(nx, seed, "polygon", axis={"principal": "z"}, sides=2, count_per_side=3)
    assert bad.error["category"] == "invalid_argument" and "sides" in bad.error["field"], bad
    extra = pattern(nx, seed, "circular", axis={"principal": "z"}, count=3, angle=10, spacing=4)
    assert extra.error["category"] == "invalid_argument", extra


def test_pattern_of_a_feature_with_its_own_body_counts_the_new_bodies():
    # NX 2406: instances of a seed that made its own body are new bodies; the seed body
    # keeps its volume.
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    seed = feature(nx, "EXTRUDE", [block])

    def commit(b):
        made = [new_body(nx, [20 * i, 0, 0, 20 * i + 10, 10, 10]) for i in (1, 2)]
        return counted(feature(nx, "PATTERN_FEATURE", []), 1 + len(made))

    nx.on_commit("PatternFeatureBuilder", commit)
    ok = pattern(nx, seed, "circular", axis={"principal": "z"}, count=3, angle=120)
    assert ok.ok, ok
    assert ok.data["count"] == 3 and ok.data["new_bodies"] == 2
    assert ok.data["volume"] == 3000 and len(ok.data["bodies"]) == 3


def _separate_copies(nx, block, n, unite_leaves=0):
    """A pattern/mirror commit that makes ``n`` separate copy bodies of ``block`` and a
    boolean commit that unites them (leaving ``unite_leaves`` separate)."""
    made = []

    def copies(kind):
        def commit(b):
            made[:] = [new_body(nx, [10 * i, 0, 0, 10 * i + 10, 10, 10]) for i in range(1, n + 1)]
            return counted(feature(nx, kind, []), 1 + n)

        return commit

    def united(b):
        assert b.Target is block and list(b.Tools.Add.calls[0][0][0]) == made
        for body in made[unite_leaves:]:
            nx.part.Bodies.remove(body)
            block.volume += body.volume
        return feature(nx, "UNITE", [block])

    nx.on_commit("BooleanBuilder", united)
    return copies, made


def test_pattern_unite_merges_the_copies_into_the_seed_body():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    seed = feature(nx, "EXTRUDE", [block])
    copies, made = _separate_copies(nx, block, 17)
    nx.on_commit("PatternFeatureBuilder", copies("PATTERN_FEATURE"))
    ok = run(nx, "nx_create_pattern", features=[nx.ref_of(seed, "feature")], layout="linear",
             parameters={"direction": [1, 0, 0], "count": 18, "spacing": 10},
             unite=True)  # fmt: skip
    assert ok.ok, ok
    data = ok.data
    assert data["new_bodies"] == 0 and data["united"] == 17 and data["count"] == 18
    assert [b["id"] for b in data["bodies"]] == [block.ref] and data["volume"] == 18000
    assert data["unite_feature"]["kind"] == "feature"
    assert data["feature"]["name"].startswith("PATTERN_FEATURE")
    assert len(ok.changes["deleted"]) == 0  # created and consumed in the same call
    assert all(b not in nx.part.Bodies for b in made) and nx.live_builders == 0


def test_pattern_unite_fails_and_rolls_back_when_copies_stay_separate():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    seed = feature(nx, "EXTRUDE", [block])
    copies, _made = _separate_copies(nx, block, 3, unite_leaves=1)
    nx.on_commit("PatternFeatureBuilder", copies("PATTERN_FEATURE"))
    bad = run(nx, "nx_create_pattern", features=[nx.ref_of(seed, "feature")], layout="linear",
              parameters={"direction": [1, 0, 0], "count": 4, "spacing": 10},
              unite=True)  # fmt: skip
    assert bad.error["code"] == "NX_BUILD_FAILED", bad
    assert bad.error["details"]["bodies"] == 2 and "touch" in bad.error["hint"]
    assert bad.mutation_outcome == "rolled_back"


def test_pattern_without_unite_keeps_the_separate_bodies_seed_first():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    seed = feature(nx, "EXTRUDE", [block])
    copies, made = _separate_copies(nx, block, 3)
    nx.on_commit("PatternFeatureBuilder", copies("PATTERN_FEATURE"))
    ok = pattern(nx, seed, "linear", direction=[1, 0, 0], count=4, spacing=10)
    assert ok.ok and ok.data["new_bodies"] == 3 and "united" not in ok.data, ok
    assert [b["id"] for b in ok.data["bodies"]] == [block.ref] + [
        nx.ref_of(b, "body") for b in made
    ]
    assert not any(b.FeatureType == "UNITE" for b in nx.part.Features)


def test_unite_needs_seed_features_of_one_body():
    nx = modeling_nx()
    a, b = nx.block(10, 10, 10), nx.block(10, 10, 10, origin=(20, 0, 0))
    seeds = [nx.ref_of(feature(nx, "EXTRUDE", [body]), "feature") for body in (a, b)]
    bad = run(nx, "nx_create_pattern", features=seeds, layout="linear",
              parameters={"direction": [1, 0, 0], "count": 2, "spacing": 50},
              unite=True)  # fmt: skip
    assert bad.error["field"] == "/unite" and "2 bodies" in bad.error["message"], bad
    assert bad.mutation_outcome == "not_started" and not any(x.commits for x in nx.builders)
    mirrored = run(nx, "nx_create_mirror", target="bodies", bodies=[a.ref],
                   plane={"principal": "yz"}, unite=True)  # fmt: skip
    assert mirrored.error["field"] == "/unite" and mirrored.mutation_outcome == "not_started"


def test_mirror_features_unite_merges_the_mirror_into_the_seed_body():
    nx = modeling_nx()
    block = nx.block(10, 10, 10)
    seed = feature(nx, "EXTRUDE", [block])
    copies, made = _separate_copies(nx, block, 1)
    nx.on_commit("MirrorBuilder", copies("MIRROR"))
    ok = run(nx, "nx_create_mirror", target="features", features=[nx.ref_of(seed, "feature")],
             plane={"principal": "yz"}, unite=True)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["new_bodies"] == 0 and ok.data["united"] == 1 and ok.data["volume"] == 2000
    assert [b["id"] for b in ok.data["bodies"]] == [block.ref] and made[0] not in nx.part.Bodies


# ---------------------------------------------------------------------------
# Short forms and guidance


def test_pattern_takes_bare_principal_directions_and_axes():
    nx, block, seed = pattern_nx()
    ok = pattern(nx, seed, "linear", direction="x", count=3, spacing=60)
    assert ok.ok, ok
    d = nx.last("PatternFeatureBuilder").PatternService.RectangularDefinition
    assert [d.XDirection.Vector.X, d.XDirection.Vector.Y] == [1.0, 0.0]
    nx.count = 6
    two = pattern(nx, seed, "linear", direction="-y", count=3, spacing=10,
                  direction2="z", count2=2, spacing2=10)  # fmt: skip
    assert two.ok, two
    d = nx.last("PatternFeatureBuilder").PatternService.RectangularDefinition
    assert d.XDirection.Vector.Y == -1.0 and d.YDirection.Vector.Z == 1.0
    circ = pattern(nx, seed, "circular", axis="z", count=6, angle=60)
    assert circ.ok, circ
    same = pattern(nx, seed, "linear", direction="x", count=2, spacing=5, direction2="-x",
                   count2=2, spacing2=5)  # fmt: skip
    assert same.error["field"] == "/parameters/direction2", same


def test_pattern_of_a_body_points_at_the_feature_that_made_it():
    nx, block, seed = pattern_nx()
    block.GetFeatures = lambda: [seed]
    bad = run(nx, "nx_create_pattern", features=[block.ref], layout="linear",
              parameters={"direction": "x", "count": 3, "spacing": 60})  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_KIND" and bad.error["field"] == "/features/0", bad
    assert nx.ref_of(seed, "feature") in bad.error["hint"], bad
    assert "nx_create_transform" in bad.error["hint"] and bad.mutation_outcome == "not_started"


def test_mirror_takes_a_bare_principal_plane():
    nx = modeling_nx()
    block = nx.block(40, 20, 10)
    nx.on_commit("MirrorBodyBuilder", lambda b: feature(nx, "MIRROR_BODY", [
        new_body(nx, [-40, 0, 0, 0, 20, 10])]))  # fmt: skip
    ok = run(nx, "nx_create_mirror", bodies=[block.ref], plane="yz")
    assert ok.ok and ok.data["volume"] == 8000 and ok.data["new_bodies"] == 1, ok
    assert len(ok.data["bodies"]) == 1  # target bodies: only the new mirrors are listed
    plane = nx.last("MirrorBodyBuilder").Plane.Value
    assert [plane.Normal.X, plane.Normal.Y, plane.Normal.Z] == [1.0, 0.0, 0.0]
    bad = run(nx, "nx_create_mirror", bodies=[nx.ref_of(block, "body")], plane="zy")
    assert bad.error["field"].startswith("/plane") and bad.mutation_outcome == "not_started"


# ---------------------------------------------------------------------------
# Pattern of whole bodies (Pattern Geometry) and unite_into


def _body_copies(nx, source, n, volume=None):
    """A Pattern Geometry commit that makes ``n`` copies of ``source`` along x."""
    made = []

    def commit(b):
        made[:] = [
            new_body(nx, [20 * i, 0, 0, 20 * i + 10, 10, 10], volume) for i in range(1, n + 1)
        ]
        return feature(nx, "PATTERN_GEOMETRY", list(made))

    nx.on_commit("PatternGeometryBuilder", commit)
    return made


def _unite_hook(nx, target, leaves=0):
    """A boolean commit that unites its tools into ``target`` (leaving ``leaves`` apart)."""
    seen = []

    def united(b):
        assert b.Target is target
        tools = list(b.Tools.Add.calls[0][0][0])
        seen[:] = tools
        for body in tools[leaves:]:
            nx.part.Bodies.remove(body)
            target.volume += body.volume
        return feature(nx, "UNITE", [target])

    nx.on_commit("BooleanBuilder", united)
    return seen


def test_pattern_of_bodies_uses_pattern_geometry_and_lists_every_copy():
    nx = modeling_nx()
    blade = nx.block(10, 10, 10)
    made = _body_copies(nx, blade, 3)
    ok = run(nx, "nx_create_pattern", bodies=[blade.ref], layout="linear",
             parameters={"direction": "x", "count": 4, "spacing": 20})  # fmt: skip
    assert ok.ok, ok
    b = nx.last("PatternGeometryBuilder")
    assert b.GeometryToPattern.Add.calls[0][0][0] == [blade] and b.destroyed
    assert b.IsAssociative is True and b.HideOriginal is False
    assert b.PatternService.RectangularDefinition.XSpacing.NCopies.RightHandSide == "4"
    data = ok.data
    assert data["count"] == 4 and data["new_bodies"] == 3 and data["volume"] == 4000
    assert [r["id"] for r in data["bodies"]] == [blade.ref] + [nx.ref_of(m, "body") for m in made]
    assert not any(x.kind == "PatternFeatureBuilder" for x in nx.builders)


def test_pattern_of_bodies_checks_the_copy_count_and_volume():
    nx = modeling_nx()
    blade = nx.block(10, 10, 10)
    _body_copies(nx, blade, 2)
    short = run(nx, "nx_create_pattern", bodies=[blade.ref], layout="linear",
                parameters={"direction": "x", "count": 4, "spacing": 20})  # fmt: skip
    assert short.error["details"]["check"] == "count", short
    assert short.mutation_outcome == "rolled_back"
    _body_copies(nx, blade, 3, volume=900.0)
    thin = run(nx, "nx_create_pattern", bodies=[blade.ref], layout="linear",
               parameters={"direction": "x", "count": 4, "spacing": 20})  # fmt: skip
    assert thin.error["details"]["check"] == "volume", thin
    assert thin.mutation_outcome == "rolled_back"


def test_pattern_of_bodies_unite_into_merges_sources_and_copies():
    nx = modeling_nx()
    disk = nx.block(100, 100, 5, origin=(-50, -50, -5))
    blade = nx.block(10, 10, 10)
    made = _body_copies(nx, blade, 5)
    tools = _unite_hook(nx, disk)
    ok = run(nx, "nx_create_pattern", bodies=[blade.ref], layout="circular",
             parameters={"axis": "z", "count": 6, "angle": 60},
             unite_into=disk.ref)  # fmt: skip
    assert ok.ok, ok
    assert tools == [blade, *made]
    data = ok.data
    assert data["united"] == 6 and data["new_bodies"] == 0 and data["count"] == 6
    assert [r["id"] for r in data["bodies"]] == [disk.ref] and data["volume"] == 50000 + 6000
    assert data["unite_feature"]["kind"] == "feature"
    assert list(nx.part.Bodies) == [disk]


def test_pattern_of_features_unite_into_merges_the_seed_body_and_copies():
    nx = modeling_nx()
    disk = nx.block(100, 100, 5, origin=(-50, -50, -5))
    blade = nx.block(10, 10, 10)
    seed = feature(nx, "LOFT", [blade])
    copies, made = _separate_copies(nx, blade, 3)
    nx.on_commit("PatternFeatureBuilder", copies("PATTERN_FEATURE"))
    tools = _unite_hook(nx, disk)
    ok = run(nx, "nx_create_pattern", features=[nx.ref_of(seed, "feature")], layout="linear",
             parameters={"direction": "x", "count": 4, "spacing": 10},
             unite_into=disk.ref)  # fmt: skip
    assert ok.ok, ok
    assert tools == [blade, *made] and ok.data["united"] == 4
    assert [r["id"] for r in ok.data["bodies"]] == [disk.ref] and ok.data["new_bodies"] == 0


def test_unite_into_copies_that_miss_the_body_rolls_back_with_a_hint():
    nx = modeling_nx()
    disk = nx.block(100, 100, 5, origin=(-50, -50, -5))
    blade = nx.block(10, 10, 10)
    _body_copies(nx, blade, 3)
    _unite_hook(nx, disk, leaves=1)
    bad = run(nx, "nx_create_pattern", bodies=[blade.ref], layout="linear",
              parameters={"direction": "x", "count": 4, "spacing": 20},
              unite_into=disk.ref)  # fmt: skip
    assert bad.error["code"] == "NX_BUILD_FAILED", bad
    assert "unite_into" in bad.error["hint"] and bad.mutation_outcome == "rolled_back"


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({}, "/features"),
        ({"features": ["F"], "bodies": ["B"]}, "/bodies"),
        ({"bodies": ["B"], "unite": True, "unite_into": "D"}, "/unite_into"),
        ({"bodies": ["B", "C"], "unite": True}, "/unite"),
        ({"bodies": ["B", "B"]}, "/bodies/1"),
    ],
)
def test_pattern_seed_and_unite_errors_are_not_started(extra, field):
    nx = modeling_nx()
    disk = nx.block(100, 100, 5, origin=(-50, -50, -5))
    blade = nx.block(10, 10, 10)
    other = nx.block(10, 10, 10, origin=(30, 0, 0))
    refs = {"B": blade.ref, "C": other.ref, "D": disk.ref,
            "F": nx.ref_of(feature(nx, "EXTRUDE", [blade]), "feature")}  # fmt: skip
    given = {k: ([refs[r] for r in v] if isinstance(v, list) else refs.get(v, v))
             for k, v in extra.items()}  # fmt: skip
    bad = run(nx, "nx_create_pattern", layout="linear",
              parameters={"direction": "x", "count": 2, "spacing": 20}, **given)  # fmt: skip
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    assert not any(b.commits for b in nx.builders)


def test_unsupported_seed_set_says_to_pattern_the_body():
    nx, block, seed = pattern_nx()
    nx.refuse("PatternFeatureBuilder", 674207, "unsupported situation")
    bad = pattern(nx, seed, "linear", direction="x", count=3, spacing=60)
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 674207, bad
    assert "bodies" in bad.error["hint"] and "body-creating feature" in bad.error["hint"]
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0


@pytest.mark.parametrize(
    "query",
    [
        "pattern geometry body copies",
        "copy bodies in a circular pattern",
        "pattern bodies",
        "body copies",
        "copy bodies along a line",
    ],
)
def test_find_tools_ranks_the_pattern_tool_first_for_body_copies(query):
    from nx_mcp.core import catalogue
    from nx_mcp.sidecar import gateways

    specs = catalogue.load()
    entries = [
        gateways.Entry(s.name, effect=s.effect, domain=s.domain, label=s.label,
                       description=s.description, keywords=s.keywords)
        for s in specs.values()
    ]  # fmt: skip
    hits, _ = gateways.ranked(entries, query, variants=lambda e: specs[e.name].variant_values())
    assert hits[0].entry.name == "nx_create_pattern", [h.entry.name for h in hits[:5]]

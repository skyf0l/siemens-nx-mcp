"""kit.features.parameter_lock: why nx_edit_feature cannot set a feature parameter, read from
the feature's own parameters (a threaded hole's thread table and radial engagement)."""

from types import SimpleNamespace as NS

from nx_mcp.kit import features


def exp(what, formula="1", locked=False, **extra):
    return NS(Description=f"(M6 x 1.0 Threaded Hole(2) {what})", RightHandSide=formula,
              IsRightHandSideLockedFromEdit=locked, **extra)  # fmt: skip


def hole(engage='"0.75"'):
    rows = [exp("Radial Engage", engage), exp("Major Diameter", "6 // Used By ..."),
            exp("Tap Drill Diameter", "5"), exp("Tip Angle", "118"), exp("Depth", "15")]  # fmt: skip
    return NS(GetExpressions=lambda: rows), rows


def test_threaded_hole_parameters_are_named_from_their_descriptions():
    assert features.hole_parameter(exp("Tap Drill Diameter")) == "tap drill diameter"
    assert features.hole_parameter(NS(Description="Block(1) Width (YC)")) is None


def test_locks_follow_the_thread_table_and_the_engagement_before_any_update():
    f, (engage, major, tap, tip, depth) = hole()
    assert features.engagement(f) is engage
    assert features.parameter_lock(f, major) == "thread_table"
    assert features.parameter_lock(f, tap) == features.parameter_lock(f, tip) == "engagement"
    assert features.parameter_lock(f, depth) is None
    custom, (_, major2, tap2, _, _) = hole('"Custom"')
    assert features.parameter_lock(custom, tap2) is None
    assert features.parameter_lock(custom, major2) == "thread_table"


def test_other_locks_are_nx_flags_and_interpart_links():
    plain = NS(GetExpressions=lambda: [])
    assert features.parameter_lock(plain, NS(Description="", IsNoEdit=True)) == "locked"
    assert features.parameter_lock(plain, NS(Description="", IsInterpartExpression=True)) == (
        "interpart"
    )
    assert features.parameter_lock(plain, NS(Description="")) is None

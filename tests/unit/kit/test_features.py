"""kit.features: timeline order without internal features, user suppression vs rolled back."""

from types import SimpleNamespace as NS

from nx_mcp.kit import features


def _feature(tag, stamp, internal=False, suppressed=False):
    return NS(Tag=tag, Timestamp=stamp, IsInternal=internal, Suppressed=suppressed)


def test_timeline_sorts_by_timestamp_and_drops_internal_features():
    a, b, csys, c = _feature(1, 2), _feature(2, 1), _feature(3, 1, internal=True), _feature(4, 2)
    part = NS(Features=[a, b, csys, c])
    assert features.timeline(part) == [b, a, c]  # ties keep NX's order
    assert features.internal(csys) and not features.internal(a)


def test_states_tell_user_suppression_from_rolled_back_features():
    a, b, c = _feature(1, 1), _feature(2, 2, suppressed=True), _feature(3, 3, suppressed=True)
    part = NS(Features=[a, b, c], CurrentFeature=b)
    masks = {1: 0, 2: features.USER_SUPPRESSED, 3: features.INACTIVE}
    tx = NS(uf=NS(Modeling=NS(AskSuppressFeature=lambda tag: masks[tag])))
    got = {tag: (s, active) for tag, (_f, s, active) in features.states(tx, part).items()}
    # c is after the current feature: NX flags it Suppressed, but the user did not.
    assert got == {1: (False, True), 2: (True, True), 3: (False, False)}
    # Without UF the flag counts only for active features.
    plain = NS(uf=NS())
    got = {t: (s, a) for t, (_f, s, a) in features.states(plain, part).items()}
    assert got == {1: (False, True), 2: (True, True), 3: (False, False)}


def test_alerts_read_warnings_in_order_without_repeats_and_never_raise():
    missed = "One or more tools did not intersect the target."
    f = NS(
        GetFeatureWarningMessages=lambda: [missed, " ", missed, "Thin wall"],
        GetFeatureErrorMessages=lambda: ["Tool body completely outside target body."],
    )
    assert features.alerts(f) == [missed, "Thin wall"]
    assert features.alerts(f, ("error", "warning"))[0].startswith("Tool body")

    def broken():
        raise RuntimeError("NX cannot answer")

    assert features.alerts(NS(GetFeatureWarningMessages=broken)) == []
    assert features.alerts(NS()) == []  # a feature without the reader

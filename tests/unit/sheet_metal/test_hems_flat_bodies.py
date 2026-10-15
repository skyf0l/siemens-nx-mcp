"""Hems that follow the part's K, formulas kept as feature formulas, and Flat Pattern
bodies refused as inputs, on the shared fake through the real kernel."""

from tests.kit import run
from tests.unit.sheet_metal.fakes import sm_nx

TOOL = "nx_create_sheet_metal_feature"


def create(nx, operation, **parameters):
    return run(nx, TOOL, operation=operation, parameters=parameters)


def hemmed(nx, body, nf):
    def hook(b):
        nx.add_bend(body, radius=0.002, angle=180.0, nf=nf)
        body.volume += 1600
        return nx.made("SB_HEM_FLANGE", body)

    return hook


def test_a_hem_follows_the_part_k_and_reliefs_unless_given():
    nx = sm_nx()  # the fake part's default K is 0.33
    body = nx.tab()
    nx.on_commit("HemFlangeFeatureBuilder", hemmed(nx, body, 0.33))
    ok = create(nx, "hem", edges=[body.edges[0].ref], length=8, style="closed")
    assert ok.ok, ok
    b = nx.last("HemFlangeFeatureBuilder")
    assert b.NeutralFactor.RightHandSide == "Sheet_Metal_Neutral_Factor"
    assert b.BendReliefDepth.RightHandSide == "Sheet_Metal_Relief_Depth"
    assert b.BendReliefWidth.RightHandSide == "Sheet_Metal_Relief_Width"
    assert b.Type == "HemFlangeBuilderTypeOptions.ClosedHemType"


def test_a_hem_takes_a_given_k_and_reads_it_back():
    nx = sm_nx()
    body = nx.tab()
    nx.on_commit("HemFlangeFeatureBuilder", hemmed(nx, body, 0.45))
    ok = create(nx, "hem", edges=[body.edges[0].ref], length=8, neutral_factor=0.45)
    assert ok.ok, ok
    assert nx.last("HemFlangeFeatureBuilder").NeutralFactor.RightHandSide == "0.45"
    assert ok.data["bends"][-1]["neutral_factor"] == 0.45
    # NX's own 0.33 instead of the K asked for: verification fails, rolled back.
    nx.on_commit("HemFlangeFeatureBuilder", hemmed(nx, body, 0.33))
    bad = create(nx, "hem", edges=[nx.ref_of(body.edges[1], "edge")], length=8,
                 neutral_factor=0.45)  # fmt: skip
    assert bad.error["details"]["check"] == "neutral_factor", bad
    assert bad.mutation_outcome == "rolled_back"


def test_an_expression_name_stays_the_flange_length_formula():
    nx = sm_nx()
    body = nx.tab()

    def flange(b):
        nx.add_bend(body)
        body.volume += 500
        return nx.made("SB_FLANGE", body)

    nx.on_commit("MultiFlangeBuilder", flange)
    ok = create(nx, "flange", edges=[body.edges[0].ref], length="Sheet_Metal_Bend_Radius")
    assert ok.ok, ok
    b = nx.last("MultiFlangeBuilder")
    entry = b.FlangePropertiesList.FeatureBendPropertiesList.Append.calls[0][0][0]
    assert entry.Length.RightHandSide == "Sheet_Metal_Bend_Radius"


def flat_body(nx):
    """A formed tab with one bend and NX 2406's two Flat Pattern bodies of it."""
    formed = nx.tab()
    bend = nx.add_bend(formed)
    pattern = nx.flat_pattern(formed, up=[bend])
    pattern.JournalIdentifier = "FLAT_PATTERN(6)"
    flats = []
    for names in (("FLAT_PATTERN(6:1B:1B)", "FLAT_PATTERN(6:1B)"), ("FLAT_PATTERN(6:2B)",)):
        flat = nx.tab()
        subs = [nx.made("EXTRACT_BODY", flat, feature_type="EXTRACT_BODY") for _ in names]
        for feature, name in zip(subs, names, strict=True):
            feature.JournalIdentifier = name
        flat.GetFeatures = lambda subs=subs: list(subs)
        flats.append(flat)
    return formed, pattern, flats


def test_edges_of_a_flat_pattern_body_are_refused_with_the_formed_body():
    nx = sm_nx()
    formed, pattern, (first, second) = flat_body(nx)
    for flat in (first, second):
        bad = create(nx, "flange", edges=[flat.edges[0].ref], length=5)
        assert bad.error["code"] == "NX_WRONG_GEOMETRY", bad
        assert bad.error["field"] == "/parameters/edges"
        assert bad.error["details"]["formed_body"]["id"] == formed.ref
        assert bad.error["details"]["flat_pattern"]["id"] == pattern.ref
        assert formed.ref in bad.error["hint"] and bad.mutation_outcome == "not_started"
    bad = create(nx, "normal_cutout", section=nx.sketch().ref, body=second.ref)
    assert bad.error["field"] == "/parameters/body" and nx.builders == []


def test_get_sheet_metal_names_the_flat_pattern_and_formed_body_of_flat_rows():
    nx = sm_nx()
    formed, pattern, _flats = flat_body(nx)
    ok = run(nx, "nx_get_sheet_metal")
    assert ok.ok, ok
    rows = ok.data["items"]
    assert rows[0]["flat_pattern"] is False and "formed_body" not in rows[0]
    for row in rows[1:]:
        assert row["flat_pattern"] is True
        assert row["flat_pattern_feature"]["id"] == pattern.ref
        assert row["formed_body"]["id"] == formed.ref

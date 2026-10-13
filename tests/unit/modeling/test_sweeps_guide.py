"""nx_create_sweep_along_guide and nx_create_variational_sweep on the shared fake."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core import evidence
from tests.kit import run
from tests.unit.modeling.fakes import feature, modeling_nx, new_body


def line(nx, length, sketch=None):
    c = nx.curve(sketch)
    c.GetLength = lambda: length
    nx.remember(c)
    return c


def sweeps(nx, kind, volume, body=None):
    def commit(_b):
        made = body or new_body(nx, [0, 0, 0, 100, 10, 5], volume)
        if body is not None:
            body.volume = volume
        return feature(nx, "SWEEP", [made])

    nx.on_commit(kind, commit)


def test_sweep_along_a_sketch_guide_fills_both_sections_and_reports_the_length():
    nx = modeling_nx()
    section, guide = nx.sketch(), nx.sketch()
    line(nx, 100.0, guide)
    sweeps(nx, "SweepAlongGuideBuilder", 5000.0)
    ok = run(nx, "nx_create_sweep_along_guide", section=section.ref, guide=guide.ref)
    assert ok.ok, ok
    b = nx.last("SweepAlongGuideBuilder")
    assert b.Section.AddToSection.calls[0][0][0] == [("CurveFeature", [section.Feature])]
    assert b.Guide.AddToSection.calls[0][0][0] == [("CurveFeature", [guide.Feature])]
    assert b.FirstOffset.RightHandSide == "0.0" and b.SecondOffset.RightHandSide == "0.0"
    assert b.BooleanOperation.Type is nx.nx.GeometricUtilities.BooleanOperation.BooleanType.Create
    assert b.DistanceTolerance == pytest.approx(0.01) and nx.live_builders == 0
    assert ok.data["volume"] == 5000 and ok.data["guide_length"] == pytest.approx(100)


def test_sweep_along_curves_with_offsets_and_subtract():
    nx = modeling_nx()
    section = nx.sketch()
    a, c = line(nx, 60.0), line(nx, 40.0)
    block = nx.block(20, 20, 20)
    sweeps(nx, "SweepAlongGuideBuilder", 7000.0, body=block)
    ok = run(nx, "nx_create_sweep_along_guide", section=section.ref, guide=[a.ref, c.ref],
             first_offset=1, second_offset=-1,
             combine={"operation": "subtract", "targets": [block.ref]})  # fmt: skip
    assert ok.ok, ok
    b = nx.last("SweepAlongGuideBuilder")
    assert b.Guide.AddToSection.calls[0][0][0] == [("CurveDumb", [a, c])]
    assert b.FirstOffset.RightHandSide == "1.0" and b.SecondOffset.RightHandSide == "-1.0"
    assert b.BooleanOperation.Type is (
        nx.nx.GeometricUtilities.BooleanOperation.BooleanType.Subtract
    )
    assert ok.data["volume"] == 7000 and ok.data["guide_length"] == pytest.approx(100)


@pytest.mark.parametrize("same", ["section", "curve"])
def test_section_and_guide_must_differ(same):
    nx = modeling_nx()
    section = nx.sketch()
    own = line(nx, 10.0, section)
    guide = section.ref if same == "section" else [own.ref]
    bad = run(nx, "nx_create_sweep_along_guide", section=section.ref, guide=guide)
    assert bad.error["field"] == "/guide" and bad.mutation_outcome == "not_started"


def test_a_sketch_guide_stands_alone():
    nx = modeling_nx()
    section, guide = nx.sketch(), nx.sketch()
    c = line(nx, 10.0)
    bad = run(nx, "nx_create_sweep_along_guide", section=section.ref, guide=[guide.ref, c.ref])
    assert bad.error["field"] == "/guide" and bad.mutation_outcome == "not_started"


def path_sketch(nx):
    sk = nx.sketch()
    sk.Feature.GetParents = lambda: [NS(FeatureType="DATUM_ON_PATH")]
    nx.session.LicenseManager = NS(CheckPresence=lambda feature: True)
    return sk


def test_variational_sweep_sets_limits_and_solid_body():
    nx = modeling_nx()
    sk = path_sketch(nx)
    sweeps(nx, "VarsweepBuilder", 1600.0)
    ok = run(nx, "nx_create_variational_sweep", sketch=sk.ref, start=10, end=60)
    assert ok.ok, ok
    b = nx.last("VarsweepBuilder")
    assert b.Section.rules == [("CurveFeature", [sk.Feature])]
    assert b.Limits.StartExtend.Value.RightHandSide == "10.0"
    assert b.Limits.EndExtend.Value.RightHandSide == "60.0"
    assert b.FeatureOptions.BodyType is (nx.nx.GeometricUtilities.FeatureOptions.BodyStyle.Solid)
    assert ok.data["volume"] == 1600 and (ok.data["start"], ok.data["end"]) == (10, 60)


def test_variational_sweep_needs_a_path_sketch_and_the_license():
    nx = modeling_nx()
    flat = nx.sketch()
    bad = run(nx, "nx_create_variational_sweep", sketch=flat.ref)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.mutation_outcome == "not_started"
    sk = path_sketch(nx)
    nx.session.LicenseManager = NS(CheckPresence=lambda feature: False)
    lic = run(nx, "nx_create_variational_sweep", sketch=sk.ref)
    assert lic.error["code"] == "NX_LICENSE_UNAVAILABLE" and not nx.builders
    evidence.forget_licenses()  # a tested tool's refusal is remembered until NX restarts
    order = run(nx, "nx_create_variational_sweep", sketch=sk.ref, start=60, end=40)
    assert order.error["field"] == "/end" and order.mutation_outcome == "not_started"

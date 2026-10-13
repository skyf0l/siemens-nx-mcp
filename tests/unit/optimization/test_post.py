"""nx_set_topology_contour on the fake: plot, limits per result and load case, hide."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.optimization import picture, post
from tests.kit import run
from tests.kit.kernel import services
from tests.unit.optimization.fakes import png, topo_nx, window_pixel
from tests.unit.optimization.test_topology import add, ready


def solved(nx, **extra):
    study, plate, holes, end = ready(nx)
    if extra:
        assert add(nx, study, "force", faces=[end.ref], **extra).ok
    assert run(nx, "nx_run_topology_study", study=study).ok
    return study


def contour(nx, study, **args):
    return run(nx, "nx_set_topology_contour", study=study, **args)


def test_stress_contour_reads_the_plotted_range_in_mpa():
    nx = topo_nx()
    study = solved(nx)
    ok = contour(nx, study)
    assert ok.ok, ok
    data = ok.data
    assert data["result"] == "stress" and data["shown"] is True
    assert data["max"] == pytest.approx(85.0) and data["min"] == 0.0 and data["unit"] == "MPa"
    assert data["load_case"] == "Default Subcase" and data["study"]["id"] == study
    b = nx.last("ViewContourBuilder")
    DS = nx.nx.DesignSimulation
    assert b.ResultComponentType is DS.ViewContourBuilder.ResultComponentOption.StressVonMises
    assert b.DisplayType is DS.ViewContourBuilder.DisplayOption.Single
    assert b.ContourDisplayStyle is DS.ViewContourBuilder.ContourDisplayStyleOption.Banded
    assert b.SubcaseIndexForFirstStudy == 0 and b.FirstStudy is nx.refs[study][0].ActiveStudy
    assert nx.live_builders == 0 and ok.mutation_outcome == "committed"


@pytest.mark.parametrize(
    ("result", "low", "high", "unit"),
    [
        ("displacement", 0.0, 0.12, "mm"),
        ("displacement_z", -0.108, 1e-4, "mm"),
        ("principal_stress", -102.0, 93.5, "MPa"),
        ("safety_factor", 250.0 / 85.0, 1e6, None),
    ],
)
def test_other_results(result, low, high, unit):
    nx = topo_nx()
    ok = contour(nx, solved(nx), result=result, style="smooth")
    assert ok.ok, ok
    assert ok.data["min"] == pytest.approx(low) and ok.data["max"] == pytest.approx(high)
    assert ok.data["unit"] == unit
    DS = nx.nx.DesignSimulation
    style = nx.last("ViewContourBuilder").ContourDisplayStyle
    assert style is DS.ViewContourBuilder.ContourDisplayStyleOption.Smooth


def test_load_case_by_name():
    nx = topo_nx()
    nx.case_peaks = [{"Displacement": 0.12, "StressVonMises": 85000.0},
                     {"Displacement": 0.3, "StressVonMises": 120000.0}]  # fmt: skip
    study = solved(nx, magnitude=50, direction="y", load_case="Side")
    ok = contour(nx, study, load_case="side")
    assert ok.ok, ok
    assert ok.data["load_case"] == "Side" and ok.data["max"] == pytest.approx(120.0)
    assert nx.last("ViewContourBuilder").SubcaseIndexForFirstStudy == 1
    unknown = contour(nx, study, load_case="slam")
    assert unknown.error["code"] == "NX_NOT_FOUND" and unknown.error["field"] == "/load_case"
    assert unknown.error["details"]["load_cases"] == ["Default Subcase", "Side"]
    assert unknown.mutation_outcome == "not_started"


def test_none_hides_the_plot_through_the_task():
    nx = topo_nx()
    study = solved(nx)
    assert contour(nx, study).ok
    post = nx.part.PostManager
    assert post.plot is not None
    env = nx.session.TopologyOptimizationTaskEnvironment
    env.log.clear()
    ok = contour(nx, study, result="none")
    assert ok.ok, ok
    assert ok.data["shown"] is False and ok.data["max"] is None
    assert post.plot is None and post.hides == 1
    assert env.log == ["Enter", "Cancel", "Exit"] and env.ActiveFeature is None
    again = contour(nx, study)  # the results survive the cancelled task
    assert again.ok and again.data["max"] == pytest.approx(85.0)


def test_refusals():
    nx = topo_nx()
    study, *_ = ready(nx)
    unsolved = contour(nx, study)
    assert unsolved.error["code"] == "NX_NOT_FOUND" and unsolved.mutation_outcome == "not_started"
    assert "nx_run_topology_study" in unsolved.error["hint"]
    nx.licenses.clear()
    refused = contour(nx, study)
    assert refused.error["code"] == "NX_LICENSE_UNAVAILABLE"


def test_a_plot_that_misses_the_peak_fails_verification():
    nx = topo_nx()
    study = solved(nx)
    post = nx.part.PostManager
    post.GetContourLimits = lambda i: (0.0, 1.0, NS(Name="StressNewtonPerSquareMilliMeter"))
    bad = contour(nx, study)
    assert bad.error["code"] == "NX_VERIFICATION_FAILED"
    assert bad.error["details"]["check"] == "max"


# -- capture: plotted inside the task, exported, the task cancelled ----------------------


def test_capture_draws_the_result_inside_the_task_and_changes_nothing():
    nx = topo_nx()
    study = solved(nx)
    env = nx.session.TopologyOptimizationTaskEnvironment
    env.log.clear()
    camera = dict(nx.camera)
    ok = contour(nx, study, result="stress", capture={"path": "shots/stress.png"})
    assert ok.ok, ok
    data = ok.data
    assert data["max"] == pytest.approx(85.0) and data["unit"] == "MPa"
    assert data["path"] == "shots/stress.png" and data["overwritten"] is False
    assert data["image"] == {"path": "shots/stress.png", "sha256": data["sha256"],
                             "inline": False, "mime_type": "image/png", "width": 60,
                             "height": 40}  # fmt: skip
    assert data["resolution"] == [60, 40] and data["contour_share"] > post.MIN_SHARE
    assert nx.exports == [{"task": True, "plotted": True, "camera": {**camera, "scale": 4.4}}]
    assert env.log == ["Enter", "Cancel", "Exit"] and env.ActiveFeature is None
    assert data["shown"] is False and nx.part.PostManager.plot is None
    assert nx.camera == camera  # the fit in the task is put back
    assert nx.live_builders == 0
    again = contour(nx, study)  # the study and its results are unchanged
    assert again.ok and again.data["max"] == pytest.approx(85.0)


def test_capture_defaults_to_captures_inline_and_takes_fit_false_and_a_load_case():
    nx = topo_nx()
    nx.case_peaks = [{"Displacement": 0.12, "StressVonMises": 85000.0},
                     {"Displacement": 0.3, "StressVonMises": 120000.0}]  # fmt: skip
    study = solved(nx, magnitude=50, direction="y", load_case="Side")
    camera = dict(nx.camera)
    ok = contour(nx, study, result="displacement", load_case="Side", style="smooth",
                 capture={"fit": False})  # fmt: skip
    assert ok.ok, ok
    assert ok.data["path"].startswith("captures/contour-") and ok.data["image"]["inline"] is True
    assert ok.data["load_case"] == "Side" and ok.data["max"] == pytest.approx(0.3)
    assert nx.exports[-1]["camera"] == camera
    assert nx.last("ViewContourBuilder").SubcaseIndexForFirstStudy == 1


def test_a_legend_only_capture_fails_verification_and_leaves_no_file():
    nx = topo_nx()
    study = solved(nx)
    nx.body_drawn = False  # NX drew the legend without the result body
    bad = contour(nx, study, capture={"path": "shots/legend.png"})
    assert bad.error["code"] == "NX_VERIFICATION_FAILED", bad
    assert bad.error["details"]["check"] == "contour_share"
    env = nx.session.TopologyOptimizationTaskEnvironment
    assert env.ActiveFeature is None and env.log[-2:] == ["Cancel", "Exit"]
    assert not list(services(nx).root.rglob("*.png"))  # neither published nor left staged


def test_capture_path_rules():
    nx = topo_nx()
    study = solved(nx)
    assert contour(nx, study, capture={"path": "shots/a.png"}).ok
    exists = contour(nx, study, capture={"path": "shots/a.png"})
    assert exists.error["code"] == "NX_ALREADY_EXISTS" and exists.mutation_outcome == "not_started"
    assert exists.error["field"] == "/capture/path"
    replaced = contour(nx, study, capture={"path": "shots/a.png", "overwrite": True})
    assert replaced.ok and replaced.data["overwritten"] is True
    orphan = contour(nx, study, capture={"overwrite": True})
    assert orphan.error["field"] == "/capture/overwrite"
    hide = contour(nx, study, result="none", capture={})
    assert hide.error["code"] == "NX_INVALID_ARGUMENT" and hide.error["field"] == "/capture"
    nx.session.IsBatch = True
    batch = contour(nx, study, capture={})
    assert batch.error["code"] == "NX_VIEWPORT_UNAVAILABLE"


@pytest.mark.parametrize("filters", [(0,), (1,), (2,), (3,), (4,), (0, 1, 2, 3, 4)])
def test_contour_share_decodes_every_png_filter(tmp_path, filters):
    nx = NS(window=(40, 30))
    file = tmp_path / "w.png"
    file.write_bytes(png(40, 30, window_pixel(nx, True, True), filters))
    with_body = picture.contour_share(file)
    file.write_bytes(png(40, 30, window_pixel(nx, True, False), filters))
    assert picture.contour_share(file) == 0.0  # the legend's colour bar is left out
    assert with_body == pytest.approx(0.0533, abs=0.01)
    width, height, _bpp, rows = picture.png_rows(file)
    assert (width, height, len(rows[0])) == (40, 30, 120)

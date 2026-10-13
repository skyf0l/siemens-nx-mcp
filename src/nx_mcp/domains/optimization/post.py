"""Show a solved topology study's results as a contour plot in the graphics window.

NX 2406 (probed live, 100x20x10 mm steel cantilever, quality 9):

- ``BasePart.PostManager.CreateViewContourBuilder`` plots a solved study's result outside
  the Topology Optimization task (the results stay with the study after the task exits):
  ``DisplayType`` Single, ``FirstStudy``, ``ResultComponentType``,
  ``SubcaseIndexForFirstStudy`` (the study's load cases in ``GetAllSubcases`` order),
  ``ContourDisplayStyle``; the commit returns None and the plot replaces the previous one.
- ``GetContourLimits(0)`` reads the plotted range with its unit: von Mises stress in
  ``StressNewtonPerSquareMilliMeter`` (max 41.420332 = the subcase result's 41420.332 kPa
  peak), displacement in the part's length unit (0.122067 mm = the peak), safety factor
  unitless (min = yield / peak stress, max capped at 1e6 where nothing is stressed).
- ``HideContour`` returns but leaves the plot (``IsContourShown`` stays true, the legend
  stays); entering the Topology Optimization task clears it, so ``none`` enters the task
  and cancels it (the study and its results stay as they were).
- A load case index without results fails ("No solved result for the specified subcase")
  and leaves the previous plot.
- Not available through NXOpen 2406 for a topology study: ``Study.AnalysisReportSettings``
  and ``Study.CreateAnalysisReportBuilder`` are refused ("Operation cannot be performed
  while in a task environment", inside the task and out; they serve Performance Predictor
  studies), and ``Study.CreateResultMeasureBuilder`` needs a separate license that a seat
  may report present yet refuse to check out, so no tool uses them.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, FeatureRef, Text
from nx_mcp.domains.optimization import common
from nx_mcp.kit import builders, readback, result, units

Result = Literal["stress", "principal_stress", "displacement", "displacement_x",
                 "displacement_y", "displacement_z", "safety_factor", "none"]  # fmt: skip
COMPONENTS = {
    "stress": "StressVonMises",
    "principal_stress": "StressWorstPrincipal",
    "displacement": "DisplacementMagnitude",
    "displacement_x": "DisplacementX",
    "displacement_y": "DisplacementY",
    "displacement_z": "DisplacementZ",
    "safety_factor": "SafetyFactor",
}
STYLES = {"banded": "Banded", "smooth": "Smooth"}
PEAK_REL = 1e-3  # contour max vs the study's own peak (NX rounds the stress to 1e-6 MPa)
LENGTHS = {"MilliMeter": "mm", "Inch": "in"}


class ContourData(TypedDict):
    study: result.Row
    result: str
    shown: bool
    load_case: str | None
    min: float | None
    max: float | None
    unit: str | None


def _case(study, load_case):
    """(index, name) of the load case to plot: ``load_case`` by name, else the first."""
    names = [str(getattr(c, "Name", "") or "") for c in common.all_of(study, "GetAllSubcases")]
    if not names:
        fail("NX_NOT_FOUND", "The study has no load case", field="/study")
    if load_case is None:
        return 0, names[0]
    wanted = load_case.strip().casefold()
    for i, name in enumerate(names):
        if name.casefold() == wanted:
            return i, name
    fail("NX_NOT_FOUND", f"No load case {load_case!r} in the study", field="/load_case",
         details={"load_cases": names})  # fmt: skip
    raise AssertionError  # unreachable: fail raises


def _limits(tx, manager, what):
    """Plotted (min, max, unit): stress in MPa, displacement in part units."""
    with native():
        low, high, unit = manager.GetContourLimits(0)
    name = str(getattr(unit, "Name", "") or "") if unit is not None else ""
    low, high = float(low), float(high)
    if what == "safety_factor" or not name:
        return low, high, None
    if what.endswith("stress"):
        factor = common.UNIT_FACTORS["pressure"].get(name)
        if factor is None:
            return low, high, name
        return low * factor, high * factor, "MPa"
    source = LENGTHS.get(name)
    if source is None:
        return low, high, name
    target = units.part_units(tx)
    scale = units.MM_PER[source] / units.MM_PER[target]
    return low * scale, high * scale, target


@tool(
    effect="view",
    label="Show topology result contour",
    summary=lambda a, say: (
        "Hide the topology result contour"
        if a.get("result") == "none"
        else f"Show {a.get('result', 'stress')} of {say('study')}"
    ),
    keywords=(
        "stress contour",
        "result plot",
        "displacement plot",
        "safety factor plot",
        "von mises",
        "fringe plot",
        "post processing",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    display_bound=True,
    example={"study": "f:12", "result": "stress"},
)
def nx_set_topology_contour(
    tx: Tx,
    *,
    study: FeatureRef,
    result: Result = "stress",
    load_case: Annotated[str, Text(40), Doc("Load case name; default the first")] | None = None,
    style: Literal["banded", "smooth"] = "banded",
) -> ContourData:
    """Plot a solved topology study's result on its body in NX for the engineer to see.
    stress (von Mises) and principal_stress in MPa, displacement in part units, safety_factor
    vs yield; returns the plotted min/max. 'none' hides the plot: do so when done. Needs a
    study solved by nx_run_topology_study (nx_analyze_topology_study keeps no results)."""
    with tx.checks():
        feature = common.feature_of(tx, study)
        common.require_license(tx)
        common.outside_task(tx)
        found = common.active_study(feature)
        manager = tx.part.PostManager
        if result != "none":
            index, name = _case(found, load_case)
            cases, _top = common.peaks(tx, found)
            if not cases:
                fail("NX_NOT_FOUND", "The study has no results", field="/study",
                     hint="Solve it with nx_run_topology_study")  # fmt: skip
    if result == "none":
        with native():
            manager.HideContour()
        with common.task(tx, feature, keep=False):
            pass  # NX 2406: entering the task clears the plot HideContour leaves
        with native():
            shown = bool(manager.IsContourShown())
        readback.expect(not shown, "shown", "NX still shows the contour")
        return {"study": tx.ref(feature, kind="feature"), "result": result, "shown": False,
                "load_case": None, "min": None, "max": None, "unit": None}  # fmt: skip
    DS = common.ds(tx)
    with builders.build(tx, manager.CreateViewContourBuilder) as b:
        b.DisplayType = DS.ViewContourBuilder.DisplayOption.Single
        b.FirstStudy = found
        b.ResultComponentType = getattr(DS.ViewContourBuilder.ResultComponentOption,
                                        COMPONENTS[result])  # fmt: skip
        b.SubcaseIndexForFirstStudy = index
        b.ContourDisplayStyle = getattr(DS.ViewContourBuilder.ContourDisplayStyleOption,
                                        STYLES[style])  # fmt: skip
        b.commit()
    with native():
        shown = bool(manager.IsContourShown())
    readback.expect(shown, "shown", "NX shows no contour")
    low, high, unit = _limits(tx, manager, result)
    peak = cases[index] if index < len(cases) else None
    want = {"stress": "max_stress", "displacement": "max_displacement"}.get(result)
    if peak is not None and want is not None and peak[want] is not None:
        readback.expect_close(high, peak[want], "max", tol=PEAK_REL * abs(peak[want]))
    return {
        "study": tx.ref(feature, kind="feature"),
        "result": result,
        "shown": shown,
        "load_case": name,
        "min": low,
        "max": high,
        "unit": unit,
    }

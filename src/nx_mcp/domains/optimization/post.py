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

``capture`` (bug-fix round, live): outside the task NX draws only the plot's legend (the
result body and its colours are missing, every band labelled 0), so nx_capture_view
cannot show a result. Inside the task, re-plotting with ``CreateViewContourBuilder``
draws the coloured result body with a correct legend, and the viewport export captures
it (Studio drops the legend; ``ShowContour`` there fails "The study is not specified";
exiting the task without ``SetCancelled`` fails "Update undo happened"). So ``capture``
enters the task, plots, fits the view (``fit``), exports the window-size PNG, then
cancels the task (study, results and model as before; NX shows no plot afterwards) and
puts the camera back (a fit in the task carries over to the model view: measured).
Without ``fit`` the task's own camera frames the body (not the engineer's; measured: the
body small in the middle). ``picture.contour_share`` checks the PNG shows the coloured
body, not the legend alone (live: fitted 0.157-0.169, unfitted 0.0079-0.0087). Each
capture takes about 3 s (the export plus the pure-Python PNG check).
"""

from __future__ import annotations

import uuid
import zlib
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, FeatureRef, PathTo, Text
from nx_mcp.domains.optimization import common, picture
from nx_mcp.kit import builders, files, readback, result, units
from nx_mcp.kit import capture as shots

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
MIN_SHARE = 0.002  # PNG share in contour colours: legend only 0.00026, a result >= 0.0079
WINDOW = (1024, 768)  # asked of the viewport export, which takes the window's own size


class Capture(TypedDict, total=False):
    path: Annotated[str, PathTo("replace", ".png")]
    overwrite: Annotated[bool, Doc("Replace an existing PNG at path")]
    inline: Annotated[bool, Doc("Return the image inline; default only without path")]
    fit: Annotated[bool, Doc("Fit the result in the PNG (default true); camera put back")]


class Image(TypedDict):
    path: str
    sha256: str
    inline: bool
    mime_type: str
    width: int
    height: int


class _Contour(TypedDict):
    study: result.Row
    result: str
    shown: bool
    load_case: str | None
    min: float | None
    max: float | None
    unit: str | None


class ContourData(_Contour, total=False):
    path: str
    size: int
    sha256: str
    overwritten: bool
    resolution: list[int]
    contour_share: float
    image: Image


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
        + (" and capture it" if a.get("capture") is not None else "")
    ),
    keywords=(
        "stress contour",
        "result plot",
        "displacement plot",
        "safety factor plot",
        "von mises",
        "fringe plot",
        "post processing",
        "result image",
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
    capture: Annotated[Capture, Doc("Save a PNG of the coloured result ({} = captures/)")]
    | None = None,
) -> ContourData:
    """Plot a solved topology study's result on its body in NX for the engineer to see.
    stress (von Mises) and principal_stress in MPa, displacement in part units, safety_factor
    vs yield; returns the plotted min/max. 'none' hides the plot: do so when done. For an
    image use capture, not nx_capture_view (NX draws only the legend outside the task):
    PNG with legend, then no plot shown. Needs a study solved by nx_run_topology_study."""
    with tx.checks():
        if capture is not None:
            target, overwrite = _target(tx, result, capture)
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
    plot = {"result": result, "index": index, "style": style}
    if capture is not None:
        return _captured(tx, feature, plot, cases, name, target, overwrite, capture)
    low, high, unit = _plot(tx, manager, found, **plot)
    with native():
        shown = bool(manager.IsContourShown())
    _peak(cases, index, result, high)
    return {
        "study": tx.ref(feature, kind="feature"),
        "result": result,
        "shown": shown,
        "load_case": name,
        "min": low,
        "max": high,
        "unit": unit,
    }


def _plot(tx, manager, found, *, result, index, style):
    """Plot ``result`` of load case ``index``; the plotted (min, max, unit)."""
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
    return _limits(tx, manager, result)


def _peak(cases, index, result, high):
    """The plotted max of stress or displacement is the study's own peak."""
    peak = cases[index] if index < len(cases) else None
    want = {"stress": "max_stress", "displacement": "max_displacement"}.get(result)
    if peak is not None and want is not None and peak[want] is not None:
        readback.expect_close(high, peak[want], "max", tol=PEAK_REL * abs(peak[want]))


def _target(tx, result, options) -> tuple[Path, bool]:
    """The PNG path of ``capture`` (default ``captures/``) and whether it replaces a file."""
    if result == "none":
        fail("NX_INVALID_ARGUMENT", "capture needs a result to plot, not none",
             field="/capture")  # fmt: skip
    if tx.session.IsBatch:
        fail("NX_VIEWPORT_UNAVAILABLE", "Captures need the interactive NX session",
             field="/capture")  # fmt: skip
    path, overwrite = options.get("path"), bool(options.get("overwrite", False))
    if overwrite and path is None:
        fail("NX_INVALID_ARGUMENT", "overwrite applies to path; give path",
             field="/capture/overwrite")  # fmt: skip
    if path is None:
        root = Path(tx.workspace.root)
        return root / "captures" / f"contour-{uuid.uuid4().hex[:12]}.png", False
    target = Path(path)
    if target.exists() and not overwrite:
        fail("NX_ALREADY_EXISTS",
             "/capture/path: a file already exists there; set overwrite=true to replace it",
             field="/capture/path", details={"path": path})  # fmt: skip
    if target.exists() and files.png_size(target) is None:
        fail("NX_UNSUPPORTED_FILE_TYPE",
             "overwrite replaces PNG images only; the file there is not a PNG",
             field="/capture/path")  # fmt: skip
    return target, overwrite


def _captured(tx, feature, plot, cases, name, target, overwrite, options) -> ContourData:
    """Plot inside the Topology Optimization task, export the window to a PNG, cancel the
    task (nothing in the study or model changes) and put the camera back."""
    manager = tx.part.PostManager
    temp = tx.files.stage(target, replace=overwrite)
    fit = bool(options.get("fit", True))
    if fit:
        tx.display.keep_camera()
    with common.task(tx, feature, keep=False) as active:
        low, high, unit = _plot(tx, manager, common.active_study(active), **plot)
        if fit:
            with native(default="NX_CAPTURE_FAILED"):
                view = tx.part.ModelingViews.WorkView
                view.Fit()
                view.UpdateDisplay()
        size = shots.viewport_png(tx, temp, *WINDOW)
    if tx.display.camera is not None:
        tx.display.restore()  # the fit carried over to the model view
    with native():
        shown = bool(manager.IsContourShown())
    try:
        share = picture.contour_share(temp)
    except (OSError, ValueError, zlib.error) as error:
        fail("NX_CAPTURE_FAILED", f"NX wrote a PNG this check cannot read: {error}")
    readback.expect(
        share >= MIN_SHARE, "contour_share",
        f"The PNG shows no coloured result body ({share:.5f} of it in contour colours)",
    )  # fmt: skip
    _peak(cases, plot["index"], plot["result"], high)
    root = Path(tx.workspace.root)
    published = tx.files.publish()[0]
    meta = files.metadata(published, root)
    inline = options.get("inline")
    return {
        "study": tx.ref(feature, kind="feature"),
        "result": plot["result"],
        "shown": shown,
        "load_case": name,
        "min": low,
        "max": high,
        "unit": unit,
        **meta,
        "overwritten": published in tx.files.replaced,
        "resolution": list(size),
        "contour_share": round(share, 5),
        "image": {
            "path": meta["path"],
            "sha256": meta["sha256"],
            "inline": options.get("path") is None if inline is None else bool(inline),
            "mime_type": "image/png",
            "width": size[0],
            "height": size[1],
        },
    }

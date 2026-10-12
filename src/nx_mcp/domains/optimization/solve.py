"""Solve a topology study and read studies back.

``Study.RunOptimization`` launches NX's optimizer as its own process and returns;
``RetrieveOptimizationResults`` waits for it and loads the optimized body (and the stress
and displacement results) into the study. The call is ``long``: one native wait that
cannot be interrupted (about 2 minutes for a 100x60x10 mm bracket at quality 9; NX does
not answer meanwhile). The optimized bodies live only while the task is open, so the result
is measured there; afterwards it is the feature's own body. The achieved mass fraction is the optimized body's volume over the
design space body's volume (same material); every support and load face is checked to
touch the optimized body (keep-in regions sit on them).
"""

from __future__ import annotations

import time
from typing import TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import FeatureRef
from nx_mcp.domains.optimization import common
from nx_mcp.kit import curve, facts, measure, readback, result, units

TOUCH_MM = 0.5  # a support or load face counts as kept when the result is this close
# MPa per NX result stress unit: results come in the part's base units, kg/(mm*s^2) = kPa
# in mm parts (live: 162061 for a 162 MPa peak) and lbm/(in*s^2) in inch parts.
STRESS_UNITS = {"mm": 1e-3, "in": 1.7858e-5}


class Kept(TypedDict):
    face: result.Row
    role: str
    gap: float
    kept: bool


class RunData(TypedDict):
    study: result.Row
    seconds: float
    bodies: list[result.Row]
    design_volume: float
    volume: float
    mass_fraction: float
    reduction_percent: float
    design_mass: float | None
    mass: float | None
    target_mass: float | None
    max_displacement: float | None
    max_stress: float | None
    kept: list[Kept]
    all_kept: bool


def _peaks(tx, study) -> tuple[float | None, float | None]:
    """``(max displacement in part units, max von Mises stress in MPa)``, when NX has them."""
    kinds = tx.nx.DesignSimulation.AnalysisBodySubcaseResults.ResultType
    best: dict[str, float] = {}
    for body in common.all_of(study, "GetAllAnalysisBodies"):
        results = getattr(body, "Results", None)
        if results is None:
            continue
        try:
            cases = list(results.GetSubcaseResults() or [])
        except Exception:
            continue
        for case in cases:
            for key, kind in (("u", kinds.Displacement), ("s", kinds.StressVonMises)):
                try:
                    if case.HasResults(kind):
                        _location, value = case.GetMaxMagnitudeAndLocation(kind)
                        best[key] = max(best.get(key, 0.0), float(value))
                except Exception:
                    continue
    stress = best.get("s")
    if stress is not None:
        stress *= STRESS_UNITS[units.part_units(tx)]
    return best.get("u"), stress


def _kept(tx, study, bodies) -> list[Kept]:
    """Each support and load face, and whether the optimized bodies still touch it."""
    limit = TOUCH_MM / units.MM_PER[units.part_units(tx)]
    rows: list[Kept] = []
    seen: set[int] = set()
    for role, getter in (("support", "GetAllAnalysisConstraints"), ("load", "GetAllAnalysisLoads")):
        for item in common.all_of(study, getter):
            with native():
                faces = list(item.GetFaces() or [])
            for face in faces:
                if int(face.Tag) in seen:
                    continue
                seen.add(int(face.Tag))
                point = facts.face_point(tx, face)
                gap = min(curve.point_distance(tx, b, point)[0] for b in bodies)
                rows.append(
                    {
                        "face": tx.ref(face, kind="face"),
                        "role": role,
                        "gap": gap,
                        "kept": gap <= limit,
                    }
                )
    return rows


def _cover_subcases(study) -> None:
    """Every subcase references every support and load (NX: "Each subcase should have at
    least one constraint and one load"); a study made in NX's dialog may lack some."""
    items = common.all_of(study, "GetAllAnalysisConstraints") + common.all_of(
        study, "GetAllAnalysisLoads"
    )
    for case in common.all_of(study, "GetAllSubcases"):
        have = {int(x.Tag) for x in list(case.GetReferences() or [])}
        for item in items:
            if int(item.Tag) not in have:
                case.AddReference(item)


def _alive(body) -> bool:
    try:
        return int(body.Tag) != 0 and bool(body.JournalIdentifier)
    except Exception:
        return False


@tool(
    effect="model",
    label="Run topology study",
    summary="Solve topology study {study}",
    keywords=("solve topology", "run optimization", "optimize", "optimized body", "lightweight"),
    requires=("work_part", "no_active_sketch"),
    cost="long",
    display_bound=True,
    example={"study": "f:12"},
)
def nx_run_topology_study(
    tx: Tx,
    *,
    study: FeatureRef,
) -> RunData:
    """Solve a topology study with the NX optimizer (minutes) and load its optimized body.
    Returns the achieved mass fraction and reduction, peak stress (MPa) and displacement,
    and whether every support and load face stays in the result."""
    with tx.checks():
        feature = common.feature_of(tx, study)
        common.require_license(tx)
        found = common.active_study(feature)
        designs = common.all_of(found, "GetAllDesignSpaces")
        if not designs:
            fail("NX_NOT_FOUND", "The study has no design space", field="/study")
        for what, getter, kind in (
            ("support", "GetAllAnalysisConstraints", "fixed"),
            ("load", "GetAllAnalysisLoads", "force"),
        ):
            if not common.all_of(found, getter):
                fail(
                    "NX_INVALID_ARGUMENT",
                    f"The study has no {what}",
                    field="/study",
                    hint=f"Add one with nx_create_topology_condition (kind {kind!r})",
                )
    started = time.monotonic()
    with common.task(tx, feature) as active:
        current = common.active_study(active)
        with native():
            _cover_subcases(current)
            current.RunOptimization()
            current.RetrieveOptimizationResults()
            bodies = list(current.GetOptimizedBodies() or [])
        seconds = time.monotonic() - started
        if not bodies:
            fail(
                "NX_INCOMPLETE_RESULT",
                "The optimizer returned no body",
                hint="Check supports and loads (each on the design space), or lower the target",
            )
        # NX 2406: the optimized (facet) bodies live while the task is open; measure here.
        design_body = common.all_of(current, "GetAllDesignSpaces")[0]
        design_volume = measure.volume(tx, [design_body.Body])
        volume = measure.volume(tx, bodies)
        readback.expect(volume > 0, "volume", volume=volume)
        design_mass = common.mass_of(design_body)
        displacement, stress = _peaks(tx, current)
        kept = _kept(tx, current, bodies)
    tx.checkpoint()
    shown = common.result_bodies(feature) or [b for b in bodies if _alive(b)]
    fraction = volume / design_volume
    target = common.target_mass(feature)
    return {
        "study": tx.ref(feature, kind="feature"),
        "seconds": round(seconds, 1),
        "bodies": result.rows(tx, shown, "body"),
        "design_volume": design_volume,
        "volume": volume,
        "mass_fraction": fraction,
        "reduction_percent": (1.0 - fraction) * 100.0,
        "design_mass": design_mass,
        "mass": None if design_mass is None else design_mass * fraction,
        "target_mass": target,
        "max_displacement": displacement,
        "max_stress": stress,
        "kept": kept,
        "all_kept": all(row["kept"] for row in kept),
    }

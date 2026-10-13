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
    load_cases: list[common.CasePeaks]
    kept: list[Kept]
    all_kept: bool


def _kept(tx, study, bodies) -> list[Kept]:
    """Each support and load face of the design space (not of scenery bodies), and whether
    the optimized bodies still touch it."""
    limit = TOUCH_MM / units.MM_PER[units.part_units(tx)]
    rows: list[Kept] = []
    seen = {int(f.Tag) for b in common.scenery_bodies(study) for f in b.GetFaces()}
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
    Returns the achieved mass fraction and reduction, peak stress (MPa) and displacement (also
    per load case), and whether every design space support and load face stays in the result."""
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
            common.cover_subcases(current)
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
        cases, top = common.peaks(tx, current)
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
        "max_displacement": top["max_displacement"],
        "max_stress": top["max_stress"],
        "load_cases": cases,
        "kept": kept,
        "all_kept": all(row["kept"] for row in kept),
    }

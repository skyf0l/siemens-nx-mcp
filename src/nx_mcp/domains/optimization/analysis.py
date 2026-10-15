"""Analyze a topology study's design as it is: linear statics, no lightening.

NX 2406 refuses ``Study.RunAnalysis`` on a topology study, inside the task and out
("Operation cannot be performed while in a task environment"; it serves Performance
Predictor studies, a separate license). So the study is solved by the optimizer with its
mass target at ``KEEP`` of the design mass: the optimizer keeps all but a sliver (live: 98%
of the volume after voxelizing), and its stress and displacement per load case are those
of the design. The task is then cancelled: the study, its target and the model are left
as they were. Live check (100x20x10 mm steel cantilever, 100 N at the tip, quality 9, 47 s):
0.088 mm tip deflection vs 0.097 mm from F L^3 / 3EI, 24.9 MPa vs 30 MPa from M c / I
(NX averages nodal stress on its voxel mesh, so peaks read low at coarse quality).
Stress is MPa, displacement and points part units; the safety factor is the design space
material's yield strength over the peak stress.
"""

from __future__ import annotations

import time
from typing import TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import FeatureRef
from nx_mcp.domains.optimization import common
from nx_mcp.kit import measure, readback, result

KEEP = 0.999  # mass target share for an analysis solve: the optimizer keeps the design


class CaseRow(common.CasePeaks):
    safety_factor: float | None


class AnalysisData(TypedDict):
    study: result.Row
    seconds: float
    analyzed_fraction: float
    material: str | None
    yield_strength: float | None
    max_stress: float
    stress_at: list[float] | None
    max_displacement: float | None
    displacement_at: list[float] | None
    safety_factor: float | None
    load_cases: list[CaseRow]


def _factor(strength, stress):
    return None if strength is None or not stress else strength / stress


@tool(
    effect="model",
    label="Analyze topology study",
    summary="Strength check of study {study}",
    keywords=(
        "stress analysis",
        "strength check",
        "von mises",
        "safety factor",
        "displacement",
        "linear static",
        "fea",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="long",
    display_bound=True,
    example={"study": "f:12"},
)
def nx_analyze_topology_study(tx: Tx, *, study: FeatureRef) -> AnalysisData:
    """Check a topology study's design strength as is, without lightening (linear statics).
    Returns peak von Mises stress (MPa), displacement and safety factor vs yield per load
    case. Needs supports and loads (nx_create_topology_condition); about a minute; the study
    is left unchanged. Voxel mesh: finer quality, closer peaks."""
    with tx.checks():
        feature = common.feature_of(tx, study)
        common.require_license(tx)
        found = common.active_study(feature)
        if not common.all_of(found, "GetAllDesignSpaces"):
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
        targets = common.mass_expressions(feature)
        if not targets:
            fail("NX_NOT_FOUND", "The study has no mass target", field="/study",
                 hint="Make the study with nx_create_topology_study")  # fmt: skip
    started = time.monotonic()
    with common.task(tx, feature, keep=False) as active:  # the solve is dropped afterwards
        current = common.active_study(active)
        design = common.all_of(current, "GetAllDesignSpaces")[0]
        full = common.mass_of(design)
        with native():
            common.cover_subcases(current)
            target = common.target_expression(active)
            if full and target is not None:
                common.drive(tx, target, KEEP * full, "mass")
            current.RunOptimization()
            current.RetrieveOptimizationResults()
            bodies = list(current.GetOptimizedBodies() or [])
        seconds = time.monotonic() - started
        cases, top = common.peaks(tx, current)
        material = getattr(design, "Material", None)
        kept = measure.volume(tx, bodies) / measure.volume(tx, [design.Body]) if bodies else 0.0
    if top["max_stress"] is None:
        fail(
            "NX_INCOMPLETE_RESULT",
            "NX's analysis returned no stress result",
            hint="Check that every support and load sits on the design space or a scenery body",
        )
    readback.expect(top["max_stress"] > 0, "max_stress", max_stress=top["max_stress"])
    strength = common.yield_strength(tx, material)
    if strength is None:
        tx.warn("NX_NOTE", "The material has no yield strength: no safety factor")
    rows: list[CaseRow] = [
        {**case, "safety_factor": _factor(strength, case["max_stress"])} for case in cases
    ]
    return {
        "study": tx.ref(feature, kind="feature"),
        "seconds": round(seconds, 1),
        "analyzed_fraction": kept,
        "material": None if material is None else str(material.Name),
        "yield_strength": strength,
        "max_stress": top["max_stress"],
        "stress_at": top["stress_at"],
        "max_displacement": top["max_displacement"],
        "displacement_at": top["displacement_at"],
        "safety_factor": _factor(strength, top["max_stress"]),
        "load_cases": rows,
    }

"""Solve a topology study and read studies back.

``Study.RunOptimization`` launches NX's optimizer as its own process and returns;
``RetrieveOptimizationResults`` waits for it and loads the optimized body (and the stress
and displacement results) into the study. The call is ``long``: one native wait that
cannot be interrupted (about 2 minutes for a 100x60x10 mm bracket at quality 9, 158 s for
a 100x20x20 mm block at the default quality; NX, and so every other caller of the bridge,
does not answer meanwhile). Progress is reported at each step (task entered, solving,
results loaded, measured); cancellation acts only between them. The optimized bodies live
only while the task is open, so the result is measured there; afterwards it is the
feature's own body. NX refuses to solve a solved, unchanged study again
(``UP_TO_DATE_CODE``): the task is cancelled and the stored result is measured on the
feature's result body outside the task (``cached``). The achieved mass fraction is the
optimized body's volume over the design space body's volume (same material). Every support and load face is checked against
the optimized body: ``gap`` is the face-to-body minimum distance (``kept`` when it touches),
``contact_share`` the share of points on a grid inside the face that the body still reaches
(a face whose centre was cut away still holds the load path at its rim).

Once solved, NX 2406 no longer updates the study (common.py): solving again after
``nx_delete_topology_results`` fails "Update undo happened" after the whole solve, so it
is refused up front (``common.refuse_solved``). ``Study.DiscardUnretrievedOptimizationResults``
is never called: outside a running optimization it blocks NX behind its "Work in
Progress" box (NX 2406, measured).
"""

from __future__ import annotations

import time
from typing import TypedDict

from nx_mcp.core.catalogue import restorer, tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import FeatureRef
from nx_mcp.domains.optimization import common
from nx_mcp.kit import curve, facts, measure, readback, result, units

TOUCH_MM = 0.5  # a support or load face counts as kept when the result is this close
GRID = 4  # contact_share: points on a GRID x GRID grid inside each face
# NX 2406 refuses to solve a study that is solved and unchanged: "The specified study is
# optimized and up-to-date. Optimization will not be rerun."
UP_TO_DATE_CODE = 4685108
SOLVED_BELOW = common.SOLVED_BELOW
STEPS = 4  # progress: entered the task, solved, results loaded, measured


class _UpToDate(Exception):
    """Leaves the task (cancelled: nothing changed) to report the stored result."""


class Kept(TypedDict):
    face: result.Row
    role: str
    gap: float
    kept: bool
    contact_share: float | None


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
    target_fraction: float | None
    max_displacement: float | None
    max_stress: float | None
    load_cases: list[common.CasePeaks]
    kept: list[Kept]
    all_kept: bool
    cached: bool


def _near(tx, bodies, point) -> float:
    return min(curve.point_distance(tx, b, point)[0] for b in bodies)


def _gap(tx, face, bodies) -> float:
    """Minimum distance from the face to the optimized bodies (0: touching); the face's
    inner point's distance when NX cannot measure face to body."""
    try:
        return max(0.0, min(curve.min_distance(tx, face, b)[0] for b in bodies))
    except NXError:
        return _near(tx, bodies, facts.face_point(tx, face))


def _share(tx, face, bodies, limit) -> float | None:
    """Share of grid points inside the face within ``limit`` of the optimized bodies."""
    try:
        points = [p for p, _normal in facts.interior_samples(tx, face, GRID)]
    except Exception:
        return None
    if not points:
        return None
    return sum(_near(tx, bodies, p) <= limit for p in points) / len(points)


def _kept(tx, study, bodies) -> list[Kept]:
    """Each support and load face of the design space (not of scenery bodies), and whether
    the optimized bodies still touch it (``gap``) and how much of it (``contact_share``)."""
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
                tx.checkpoint()
                gap = _gap(tx, face, bodies)
                share = _share(tx, face, bodies, limit)
                rows.append(
                    {
                        "face": tx.ref(face, kind="face"),
                        "role": role,
                        "gap": gap,
                        "kept": gap <= limit,
                        "contact_share": None if share is None else round(share, 3),
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
    """Solve a topology study with the NX optimizer and load its optimized body. Returns the
    achieved vs target mass fraction, reduction, peak stress (MPa) and displacement (also per
    load case), and whether the result still touches each support and load face. Takes 1-3
    min for a small part at quality 9 (NX and other callers wait; progress in
    nx_get_operation). Solving again returns the stored result (cached: true); after
    nx_delete_topology_results NX cannot solve it again: make a new study."""
    with tx.checks():
        feature = common.feature_of(tx, study)
        common.require_license(tx)
        found = common.active_study(feature)
        designs = common.all_of(found, "GetAllDesignSpaces")
        if not designs:
            fail("NX_NOT_FOUND", "The study has no design space", field="/study")
        solved_before = common.has_results(found)
        if not common.is_solved(tx, feature, found):  # a solved one: its stored result
            common.refuse_solved(tx, feature, "solve again", found)
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
    tx.checkpoint(0, STEPS)
    try:
        with common.task(tx, feature) as active:
            current = common.active_study(active)
            with native():
                common.cover_subcases(current)
            _solve(tx, current)
            with native():
                bodies = list(current.GetOptimizedBodies() or [])
            seconds = time.monotonic() - started
            if not bodies:
                fail(
                    "NX_INCOMPLETE_RESULT",
                    "The optimizer returned no body",
                    hint="Check supports and loads (each on the design space), or lower the target",
                )
            # NX 2406: the optimized (facet) bodies live while the task is open; measure here.
            measured = _measure(tx, current, bodies)
    except _UpToDate:
        return _stored(tx, feature, time.monotonic() - started)
    tx.checkpoint(STEPS, STEPS)
    shown = common.result_bodies(feature) or [b for b in bodies if _alive(b)]
    tx.note_restore(
        {"study": tx.ref(feature, kind="feature")["id"], "solved_before": solved_before}
    )
    return _report(tx, feature, shown, measured, seconds, cached=False)


def _solve(tx, study) -> None:
    """Run the optimizer and load its result; ``_UpToDate`` when NX refuses to solve a
    solved, unchanged study again."""
    tx.checkpoint(1, STEPS)  # not between the two: the optimizer runs on its own meanwhile
    try:
        with native():
            study.RunOptimization()
            study.RetrieveOptimizationResults()
    except NXError as error:
        if error.nx_code == UP_TO_DATE_CODE:
            raise _UpToDate from error
        raise
    tx.checkpoint(2, STEPS)


def _measure(tx, study, bodies) -> dict:
    """Volumes, masses, peaks and kept faces of the optimized ``bodies``."""
    design_body = common.all_of(study, "GetAllDesignSpaces")[0]
    design_volume = measure.volume(tx, [design_body.Body])
    volume = measure.volume(tx, bodies)
    readback.expect(volume > 0, "volume", volume=volume)
    cases, top = common.peaks(tx, study)
    tx.checkpoint(3, STEPS)
    return {
        "design_volume": design_volume,
        "volume": volume,
        "design_mass": common.mass_of(design_body),
        "cases": cases,
        "top": top,
        "kept": _kept(tx, study, bodies),
    }


def _stored(tx, feature, seconds) -> RunData:
    """The result of a solved study NX keeps (it does not solve an unchanged study again):
    the feature's result body, measured outside the task, with the stored peaks."""
    study = common.active_study(feature)
    bodies = common.result_bodies(feature, study)
    design = common.all_of(study, "GetAllDesignSpaces")[0]
    try:
        volume = measure.volume(tx, bodies) if bodies else 0.0
    except NXError:  # NX's empty result body (an unsolved study) has no volume
        volume = 0.0
    if not 0.0 < volume < SOLVED_BELOW * measure.volume(tx, [design.Body]):
        fail("NX_INCOMPLETE_RESULT", "NX reports the study solved and up to date but shows no "
             "result body", field="/study", nx_code=UP_TO_DATE_CODE,
             hint="Change a condition, the target or the quality to solve it again")  # fmt: skip
    tx.warn("NX_NOTE", "The study is solved and unchanged since: NX keeps its result and "
            "does not solve again (cached: true). Change a condition, the mass target or the "
            "quality to solve it anew")  # fmt: skip
    measured = _measure(tx, study, bodies)
    tx.checkpoint(STEPS, STEPS)
    return _report(tx, feature, bodies, measured, seconds, cached=True)


def _report(tx, feature, shown, m, seconds, *, cached) -> RunData:
    fraction = m["volume"] / m["design_volume"]
    design_mass = m["design_mass"]
    target = common.target_mass(feature)
    target_fraction = target / design_mass if target and design_mass else None
    return {
        "study": tx.ref(feature, kind="feature"),
        "seconds": round(seconds, 1),
        "bodies": result.rows(tx, shown, "body"),
        "design_volume": m["design_volume"],
        "volume": m["volume"],
        "mass_fraction": fraction,
        "reduction_percent": (1.0 - fraction) * 100.0,
        "design_mass": design_mass,
        "mass": None if design_mass is None else design_mass * fraction,
        "target_mass": target,
        "target_fraction": target_fraction,
        "max_displacement": m["top"]["max_displacement"],
        "max_stress": m["top"]["max_stress"],
        "load_cases": m["cases"],
        "kept": m["kept"],
        "all_kept": all(row["kept"] for row in m["kept"]),
        "cached": cached,
    }


def discard(tx, feature) -> None:
    """Drop the study's solved results; the task's update then empties its result body
    (NX 2406: ``DiscardOptimizationResults`` works outside the task only). The study keeps
    ``common.SOLVED_RECORD``: NX cannot solve or change it again."""
    with native():
        common.active_study(feature).DiscardOptimizationResults()
    common.mark_solved(tx, feature)
    with common.task(tx, feature):
        pass
    readback.expect(not common.has_results(common.active_study(feature)), "results",
                    "NX kept the study's results")  # fmt: skip


class ClearData(TypedDict):
    study: result.Row
    deleted: bool
    solved: bool


@tool(
    effect="model",
    label="Delete topology results",
    summary="Delete the results of {study}",
    keywords=(
        "clear results",
        "discard result",
        "reset study",
        "unsolved",
        "remove optimized body",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    display_bound=True,
    example={"study": "f:12"},
)
def nx_delete_topology_results(tx: Tx, *, study: FeatureRef) -> ClearData:
    """Delete a topology study's solved result: its result body empties; supports, loads and
    settings stay. NX cannot solve or change the study again afterwards: for a new result
    make a new study (nx_create_topology_study). deleted: false when it held none.
    nx_delete_objects deletes the study itself."""
    with tx.checks():
        feature = common.feature_of(tx, study)
        common.require_license(tx)
        common.outside_task(tx)
        solved = common.has_results(common.active_study(feature))
    if solved:
        discard(tx, feature)
    else:
        tx.warn("NX_NOTE", "The study holds no result: nothing to delete")
    return {"study": tx.ref(feature, kind="feature"), "deleted": solved, "solved": False}


@restorer("nx_run_topology_study")
def _restore_solve(tx: Tx, data: dict, *, apply: bool) -> str:
    """Drop the result an ``nx_run_topology_study`` made (the compensating undo); refused
    when the study held an earlier result (NX cannot bring it back) or no longer holds one."""
    ref = data["study"]
    if data["solved_before"]:
        fail("NX_UNSUPPORTED_OPTION", f"{ref} held an earlier result the solve replaced: NX "
             "cannot bring it back", hint="nx_delete_topology_results drops the current result")  # fmt: skip
    feature = common.feature_of(tx, ref)
    if not common.has_results(common.active_study(feature)):
        fail("NX_DEPENDENCY_ORDER", f"The result of {ref} is gone since: nothing to drop")
    if apply:
        common.require_license(tx)
        discard(tx, feature)
    return f"the result of {ref} dropped (the study is unsolved again)"

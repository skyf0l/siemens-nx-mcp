"""Shared topology-optimization helpers: license check, task environment, study lookup.

NX 2406 (probed live):

- A study lives in a Topology Optimization feature (``Features.TopologyOptimizationFeature``)
  that NX creates and edits only inside its task environment
  (``session.TopologyOptimizationTaskEnvironment``: ``SetFeatureToEdit`` + ``Enter`` ...
  ``Exit``; ``SetCancelled`` before ``Exit`` discards the task's changes). The feature ref
  (kind ``feature``) is the study's ref; ``nx_delete_objects`` deletes it.
- Every study call checks the license first (``LicenseManager.CheckPresence``): entering the
  task without ``des_top_opt`` would open NX's blocking "License Not Available" dialog.
- Builders take typed ``Null`` arguments (``Study.Null``, not None). Support, mass-target
  and shape-constraint builders commit but return None; ``GetAllOptimizationConstraints``
  never lists the mass target, so it is read back as the feature's Kilogram expression.
- A linear-statics solve needs every support and load referenced by a subcase (NX puts a
  new one in the active subcase only); the condition and run tools add them.
- ``Study.RunAnalysis`` is refused on a topology study ("Operation cannot be performed while
  in a task environment", in the task and out): see analysis.py.
- Keep-in/out faces that do not touch fail ("Selected faces are not connected"): one region
  per face. The feature owns a result body from the start, empty until solved.
- Expressions are written in their own NX units (Newton, PressureNewtonPerSquareMilliMeter,
  Kilogram, MilliMeter; inch parts convert): forces are N, pressures MPa, masses kg.
- A bracket 100x60x10 mm at quality 9 solves in about 2 minutes (NX busy meanwhile); the
  solve needs only des_top_opt (no Simcenter solver license or dialog).
"""

from __future__ import annotations

import contextlib
import importlib
from typing import TypedDict

from nx_mcp.core.errors import fail, native
from nx_mcp.kit import geom, licenses, result, select, units

# Either feature unlocks Topology Optimization for Designers (NXOpen.xml license notes).
LICENSES = ("des_top_opt", "sc_des_topol_opt")
LICENSE_HINT = (
    "Ask the engineer for the Topology Optimization for Designers license (feature "
    "des_top_opt, a Simcenter token add-on)"
)
TYPE_NAME = "TopologyOptimizationFeature"


def ds(tx):
    """``NXOpen.DesignSimulation``: a submodule NX imports only on demand (a fresh NX session
    lacks the attribute until something imports it)."""
    module = getattr(tx.nx, "DesignSimulation", None)
    if module is None:
        with native():
            module = importlib.import_module(tx.nx.__name__ + ".DesignSimulation")
    return module


class StudyRow(TypedDict):
    study: result.Row
    name: str


def require_license(tx) -> str:
    """The license feature that unlocks topology optimization, else NX_LICENSE_UNAVAILABLE
    before NX is asked to enter the task (no blocking license dialog)."""
    found = licenses.require(
        tx,
        LICENSES,
        "Topology Optimization for Designers is not licensed on this NX seat "
        f"(none of {', '.join(LICENSES)} is present)",
        hint=LICENSE_HINT,
        strict=True,
    )
    assert found is not None  # strict: refused when NX cannot tell
    return found


def is_study(feature) -> bool:
    """True for a Topology Optimization feature."""
    if type(feature).__name__ == TYPE_NAME:
        return True
    return hasattr(feature, "GetAllStudies") and hasattr(feature, "CreateStudyBuilder")


def feature_of(tx, ref, field="/study"):
    """The work part's Topology Optimization feature named by ``ref``."""
    feature = select.one(tx, ref, "feature", field=field)
    if not is_study(feature):
        fail(
            "NX_WRONG_KIND",
            f"{ref} is not a topology optimization study",
            field=field,
            hint="Pass the study ref nx_create_topology_study returned "
            "(nx_get_topology_study lists them)",
        )
    return feature


def studies(tx) -> list:
    """The work part's Topology Optimization features, in timeline order."""
    return [f for f in tx.part.Features if is_study(f)]


def active_study(feature, field="/study"):
    """The feature's active study (each feature this tool set makes holds one)."""
    with native():
        study = feature.ActiveStudy
        if study is None:
            found = list(feature.GetAllStudies() or [])
            study = found[0] if found else None
    if study is None:
        fail("NX_NOT_FOUND", "The topology optimization feature holds no study", field=field)
    return study


def all_of(study, getter: str) -> list:
    """``study.GetAll<...>()`` as a list (NX answers None for none)."""
    with native():
        return list(getattr(study, getter)() or [])


def outside_task(tx) -> None:
    """``NX_WRONG_APPLICATION`` while NX is in a Topology Optimization task (the engineer's)."""
    with native():
        if tx.session.TopologyOptimizationTaskEnvironment.ActiveFeature is not None:
            fail(
                "NX_WRONG_APPLICATION",
                "NX is already in a Topology Optimization task",
                hint="Ask the engineer to finish the Topology Optimization task in NX",
            )


@contextlib.contextmanager
def task(tx, feature=None, keep=True):
    """Inside NX's Topology Optimization task environment; yields its active feature.

    ``feature`` edits an existing study, else NX creates a new feature. A failure inside
    cancels the task (NX discards its changes) before the kernel's rollback; so does the
    end of the block without ``keep`` (a solve whose results are read, then dropped).
    """
    environment = tx.session.TopologyOptimizationTaskEnvironment
    outside_task(tx)
    with native():
        if feature is not None:
            environment.SetFeatureToEdit(feature)
        environment.Enter()
    try:
        with native():
            active = environment.ActiveFeature
        if active is None:
            fail("NX_BUILD_FAILED", "NX entered the topology task without a feature")
        yield active
    except BaseException:
        with contextlib.suppress(Exception):
            environment.SetCancelled()
        with contextlib.suppress(Exception):
            environment.Exit()
        raise
    with native():
        if not keep:
            environment.SetCancelled()
        environment.Exit()


def mass_of(analysis_body) -> float | None:
    """An analysis body's mass in kilograms (NX reports it in the part's base units)."""
    try:
        value, unit = analysis_body.GetMass()
    except Exception:
        return None
    name = str(getattr(unit, "Abbreviation", None) or getattr(unit, "Name", "") or "kg")
    factor = {"kg": 1.0, "Kilogram": 1.0, "lbm": 0.45359237, "PoundMass": 0.45359237}.get(name)
    if factor is None:
        factor = 1.0 if "kilo" in name.casefold() or name == "kg" else 0.45359237
    return float(value) * factor


# NX unit names of the study expressions -> SI-ish value per unit (N, MPa, kg, mm).
UNIT_FACTORS = {
    "force": {"Newton": 1.0, "KiloNewton": 1e3, "MilliNewton": 1e-3, "PoundForce": 4.4482216152605},
    "pressure": {
        "PressureNewtonPerSquareMilliMeter": 1.0,
        "PressureMegaPascal": 1.0,
        "PressurePascal": 1e-6,
        "PressurePoundForcePerSquareInch": 0.00689475729,
        "StressMilliNewtonPerSquareMilliMeter": 1e-3,  # material tables (kPa)
        "StressNewtonPerSquareMilliMeter": 1.0,
        "StressPoundForcePerSquareInch": 0.00689475729,
    },
    "acceleration": {  # mm/s^2
        "MeterPerSquareSecond": 1e3,  # the study's gravity, also in inch parts
        "MilliMeterPerSquareSecond": 1.0,
        "InchPerSquareSecond": 25.4,
        "FootPerSquareSecond": 304.8,
    },
    "mass": {"Kilogram": 1.0, "Gram": 1e-3, "PoundMass": 0.45359237},
    "length": {"MilliMeter": 1.0, "Inch": 25.4, "Meter": 1000.0, "CentiMeter": 10.0},
}  # fmt: skip
SI_UNITS = {"force": "Newton", "pressure": "PressureNewtonPerSquareMilliMeter",
            "mass": "Kilogram", "length": "MilliMeter",
            "acceleration": "MilliMeterPerSquareSecond"}  # fmt: skip
G_MM = 9806.65  # mm/s^2 in one standard g


def _unit_name(expression) -> str:
    unit = getattr(expression, "Units", None)
    return str(getattr(unit, "Name", "") or "")


def drive(tx, expression, value, dim):
    """Set a study expression to ``value`` given in N, MPa, kg or mm (``dim``): written in
    the expression's own unit; an unknown unit gets ``EditWithUnits`` in the SI unit."""
    with native():
        factor = UNIT_FACTORS[dim].get(_unit_name(expression))
        if factor is not None:
            expression.RightHandSide = repr(float(value) / factor)
            return
        part = tx.part
        unit = part.UnitCollection.FindObject(SI_UNITS[dim])
        part.Expressions.EditWithUnits(expression, unit, repr(float(value)))


def value_of(expression, dim) -> float | None:
    """A study expression's value in N, MPa, kg or mm (None in an unknown unit)."""
    factor = UNIT_FACTORS[dim].get(_unit_name(expression))
    if factor is None:
        return None
    try:
        return float(expression.RightHandSide) * factor
    except (TypeError, ValueError):
        return None


def length(tx, expression, value):
    """Set a length expression to ``value`` in work-part units."""
    drive(tx, expression, float(value) * units.MM_PER[units.part_units(tx)], "length")


def mass_expressions(feature) -> list:
    """The feature's mass expressions: NX lists no mass-target object, so the target's
    ``Mass`` is the feature's (last) mass expression."""
    try:
        expressions = list(feature.GetExpressions() or [])
    except Exception:
        return []
    return [e for e in expressions if _unit_name(e) in UNIT_FACTORS["mass"]]


def target_mass(feature) -> float | None:
    """The study's mass target in kg (``mass_expressions``)."""
    masses = mass_expressions(feature)
    return value_of(masses[-1], "mass") if masses else None


def design_faces(tx, refs, field, body, others=()):
    """Faces of the design space ``body`` or of ``others`` (the study's scenery bodies);
    ``NX_WRONG_OWNER`` for faces of any other body."""
    faces = select.faces(tx, refs, field)
    allowed = {int(body.Tag), *(int(b.Tag) for b in others)}
    for i, face in enumerate(faces):
        owner = face.GetBody() if hasattr(face, "GetBody") else None
        if owner is not None and int(owner.Tag) not in allowed:
            fail(
                "NX_WRONG_OWNER",
                "Select faces of the design space" + (" or a scenery body" if others else " body"),
                field=f"{field}/{i}",
            )
    return faces


def scenery_bodies(study) -> list:
    """The bodies of the study's scenery bodies (non-design bodies that carry load)."""
    return [s.Body for s in all_of(study, "GetAllSceneryBodies")]


def result_bodies(feature, study=None) -> list:
    """The solved result: the feature's bodies other than its design space and scenery
    bodies (NX 2406 lists the design body under the feature too, solved or not)."""
    try:
        bodies = list(feature.GetBodies() or [])
    except Exception:
        return []
    study = study if study is not None else active_study(feature)
    inputs = {int(d.Body.Tag) for d in all_of(study, "GetAllAnalysisBodies")}
    inputs |= {int(d.Body.Tag) for d in all_of(study, "GetAllDesignSpaces")}
    return [b for b in bodies if int(b.Tag) not in inputs]


# MPa per NX result stress unit: results come in the part's base units, kg/(mm*s^2) = kPa
# in mm parts (live: 162061 for a 162 MPa peak) and lbm/(in*s^2) in inch parts.
STRESS_UNITS = {"mm": 1e-3, "in": 1.7858e-5}


class CasePeaks(TypedDict):
    name: str
    max_stress: float | None
    max_displacement: float | None


def _where(location) -> list[float] | None:
    """``geom.xyz`` of a result location; None when NX gives none."""
    try:
        return geom.xyz(location)
    except Exception:
        return None


def peaks(tx, study) -> tuple[list[CasePeaks], dict]:
    """Solved results per load case (NX subcase order) over every analysis body: max von
    Mises stress (MPa) and displacement (part units); plus the overall peaks with the
    point (part units) where each occurs (``stress_at``, ``displacement_at``)."""
    kinds = ds(tx).AnalysisBodySubcaseResults.ResultType
    names = [str(getattr(c, "Name", "") or "") for c in all_of(study, "GetAllSubcases")]
    scale = {"s": STRESS_UNITS[units.part_units(tx)], "u": 1.0}
    cases: dict[int, dict] = {}
    top: dict = {}
    for body in all_of(study, "GetAllAnalysisBodies"):
        results = getattr(body, "Results", None)
        if results is None:
            continue
        try:
            found = list(results.GetSubcaseResults() or [])
        except Exception:
            continue
        for i, case in enumerate(found):
            row = cases.setdefault(i, {})
            for key, kind in (("u", kinds.Displacement), ("s", kinds.StressVonMises)):
                try:
                    if not case.HasResults(kind):
                        continue
                    location, value = case.GetMaxMagnitudeAndLocation(kind)
                except Exception:
                    continue
                value = float(value) * scale[key]
                row[key] = max(row.get(key, value), value)
                if value >= top.get(key, (float("-inf"), None))[0]:
                    top[key] = (value, _where(location))
    rows: list[CasePeaks] = [
        {
            "name": names[i] if i < len(names) else f"Subcase {i + 1}",
            "max_stress": cases[i].get("s"),
            "max_displacement": cases[i].get("u"),
        }
        for i in sorted(cases)
    ]
    overall = {
        "max_stress": top["s"][0] if "s" in top else None,
        "stress_at": top["s"][1] if "s" in top else None,
        "max_displacement": top["u"][0] if "u" in top else None,
        "displacement_at": top["u"][1] if "u" in top else None,
    }
    return rows, overall


def yield_strength(tx, material) -> float | None:
    """The material's yield strength in MPa (its property table), None when it has none."""
    if material is None:
        return None
    try:
        value, unit = material.GetPropTable().GetBaseScalarWithDataPropertyValue("YieldStrength")
        value = float(value)
    except Exception:
        return None
    name = str(getattr(unit, "Name", "") or "")
    factor = UNIT_FACTORS["pressure"].get(name)
    if factor is None:
        try:
            collection = tx.part.UnitCollection
            wanted = collection.FindObject(SI_UNITS["pressure"])
            return float(collection.Convert(unit, wanted, value)) or None
        except Exception:
            return None
    return value * factor or None


def uncovered(study) -> bool:
    """Whether a load case lacks a support or a load (``cover_subcases`` would add them)."""
    groups = [{int(x.Tag) for x in all_of(study, g)}
              for g in ("GetAllAnalysisConstraints", "GetAllAnalysisLoads")]  # fmt: skip
    for case in all_of(study, "GetAllSubcases"):
        have = {int(x.Tag) for x in list(case.GetReferences() or [])}
        if any(not have & group for group in groups):
            return True
    return False


def cover_subcases(study) -> None:
    """Every load case gets a support and a load (NX: "Each subcase should have at least one
    constraint and one load"): one without supports takes every support, one without loads
    every load (NX's default case starts empty; a study made in NX's dialog may lack some)."""
    supports = all_of(study, "GetAllAnalysisConstraints")
    loads = all_of(study, "GetAllAnalysisLoads")
    for case in all_of(study, "GetAllSubcases"):
        have = {int(x.Tag) for x in list(case.GetReferences() or [])}
        for group in (supports, loads):
            if not any(int(x.Tag) in have for x in group):
                for item in group:
                    case.AddReference(item)

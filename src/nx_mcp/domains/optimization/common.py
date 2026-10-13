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
- A linear-statics solve needs every support and load referenced by a subcase (the study's
  default subcase starts empty); the condition and run tools add them.
- Keep-in/out faces that do not touch fail ("Selected faces are not connected"): one region
  per face. The feature owns a result body from the start, empty until solved.
- Expressions are written in their own NX units (Newton, PressureNewtonPerSquareMilliMeter,
  Kilogram, MilliMeter; inch parts convert): forces are N, pressures MPa, masses kg.
- A bracket 100x60x10 mm at quality 9 solves in about 2 minutes (NX busy meanwhile); the
  solve needs only des_top_opt (no Simcenter solver license or dialog).
"""

from __future__ import annotations

import contextlib
from typing import TypedDict

from nx_mcp.core.errors import fail, native
from nx_mcp.kit import licenses, result, select, units

# Either feature unlocks Topology Optimization for Designers (NXOpen.xml license notes).
LICENSES = ("des_top_opt", "sc_des_topol_opt")
LICENSE_HINT = (
    "Ask the engineer for the Topology Optimization for Designers license (feature "
    "des_top_opt, a Simcenter token add-on)"
)
TYPE_NAME = "TopologyOptimizationFeature"


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


@contextlib.contextmanager
def task(tx, feature=None):
    """Inside NX's Topology Optimization task environment; yields its active feature.

    ``feature`` edits an existing study, else NX creates a new feature. A failure inside
    cancels the task (NX discards its changes) before the kernel's rollback.
    """
    environment = tx.session.TopologyOptimizationTaskEnvironment
    with native():
        if environment.ActiveFeature is not None:
            fail(
                "NX_WRONG_APPLICATION",
                "NX is already in a Topology Optimization task",
                hint="Ask the engineer to finish the Topology Optimization task in NX",
            )
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
    },
    "mass": {"Kilogram": 1.0, "Gram": 1e-3, "PoundMass": 0.45359237},
    "length": {"MilliMeter": 1.0, "Inch": 25.4, "Meter": 1000.0, "CentiMeter": 10.0},
}  # fmt: skip
SI_UNITS = {"force": "Newton", "pressure": "PressureNewtonPerSquareMilliMeter",
            "mass": "Kilogram", "length": "MilliMeter"}  # fmt: skip


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


def target_mass(feature) -> float | None:
    """The study's mass target in kg: NX lists no mass-target object, so it is the
    feature's one mass expression (the target's ``Mass``)."""
    try:
        expressions = list(feature.GetExpressions() or [])
    except Exception:
        return None
    masses = [e for e in expressions if _unit_name(e) in UNIT_FACTORS["mass"]]
    return value_of(masses[-1], "mass") if masses else None


def design_faces(tx, refs, field, body):
    """Faces of the work part's design space ``body`` (``wrong_owner`` for others)."""
    faces = select.faces(tx, refs, field)
    for i, face in enumerate(faces):
        owner = face.GetBody() if hasattr(face, "GetBody") else None
        if owner is not None and int(owner.Tag) != int(body.Tag):
            fail("NX_WRONG_OWNER", "Select faces of the design space body", field=f"{field}/{i}")
    return faces


def result_bodies(feature, study=None) -> list:
    """The solved result: the feature's bodies other than its design space body (NX 2406
    lists the design body under the feature too, solved or not)."""
    try:
        bodies = list(feature.GetBodies() or [])
    except Exception:
        return []
    study = study if study is not None else active_study(feature)
    designs = {int(d.Body.Tag) for d in all_of(study, "GetAllDesignSpaces")}
    return [b for b in bodies if int(b.Tag) not in designs]

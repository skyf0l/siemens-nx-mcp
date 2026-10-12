"""Read topology studies: list the work part's studies, or describe one."""

from __future__ import annotations

from typing import TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import FeatureRef
from nx_mcp.domains.optimization import common
from nx_mcp.kit import measure, result

LOAD_TYPES = ("Force", "BearingLoad", "Pressure", "Torque", "Acceleration",
              "EnforcedDisplacement", "RemoteForce", "HeatFlux")  # fmt: skip
SOLVED_BELOW = 0.999  # result volume / design volume of a solved study
SUPPORT_TYPES = ("Fixed", "Pinned", "PinnedSlider", "LinearSlider", "PlanarSlider", "Temperature")


class Item(TypedDict):
    type: str
    faces: int


class StudyInfo(TypedDict):
    study: result.Row
    name: str
    design_space: result.Row | None
    material: str | None
    design_mass: float | None
    target_mass: float | None
    supports: list[Item]
    loads: list[Item]
    quality: int | None
    solved: bool
    mass_fraction: float | None
    bodies: list[result.Row]


class StudyList(TypedDict):
    items: list[StudyInfo]


def _type(value, enum, names) -> str:
    return result.enum_name(value, enum, names).lower()


def _items(tx, study, getter, enum, names) -> list[Item]:
    out: list[Item] = []
    for item in common.all_of(study, getter):
        try:
            count = len(list(item.GetFaces() or []))
        except Exception:
            count = 0
        out.append({"type": _type(item.Type, enum, names), "faces": count})
    return out


def describe(tx, feature) -> StudyInfo:
    """One study's definition and solve state."""
    DS = tx.nx.DesignSimulation
    study = common.active_study(feature)
    designs = common.all_of(study, "GetAllDesignSpaces")
    design = designs[0] if designs else None
    material = getattr(design, "Material", None) if design is not None else None
    # NX 2406 gives the feature its own result body at once (a copy of the design space
    # body is empty until solved); a solved body is lighter than the design space.
    bodies = common.result_bodies(feature, study)
    fraction = None
    if bodies and design is not None:
        fraction = measure.volume(tx, bodies) / measure.volume(tx, [design.Body])
    solved = fraction is not None and 0.0 < fraction < SOLVED_BELOW
    return {
        "study": tx.ref(feature, kind="feature"),
        "name": str(getattr(study, "Name", "") or ""),
        "design_space": None if design is None else tx.ref(design.Body, kind="body"),
        "material": None if material is None else str(material.Name),
        "design_mass": None if design is None else common.mass_of(design),
        "target_mass": common.target_mass(feature),
        "supports": _items(
            tx,
            study,
            "GetAllAnalysisConstraints",
            DS.AnalysisConstraintBuilder.ConstraintType,
            SUPPORT_TYPES,
        ),  # fmt: skip
        "loads": _items(
            tx, study, "GetAllAnalysisLoads", DS.AnalysisLoadBuilder.AnalysisLoadType, LOAD_TYPES
        ),
        "quality": int(study.StudyQuality) if hasattr(study, "StudyQuality") else None,
        "solved": solved,
        "mass_fraction": fraction if solved else None,
        "bodies": result.rows(tx, bodies, "body"),
    }


@tool(
    effect="read",
    label="Get topology study",
    summary=lambda a, say: (
        f"Topology study {say('study')}" if a.get("study") else "List topology studies"
    ),
    keywords=("topology study", "optimization study", "study status", "topology result"),
    requires=("work_part",),
    example={"study": "f:12"},
)
def nx_get_topology_study(tx: Tx, *, study: FeatureRef | None = None) -> StudyList:
    """List the work part's topology optimization studies, or describe one. Rows: design
    space, material, mass and target (kg), supports, loads, quality, solved result."""
    if study is not None:
        return {"items": [describe(tx, common.feature_of(tx, study))]}
    return {"items": [describe(tx, f) for f in common.studies(tx)]}

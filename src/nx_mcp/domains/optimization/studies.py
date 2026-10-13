"""Create a Topology Optimization for Designers study.

``nx_create_topology_study`` makes the feature, its study (``StudyBuilder``: objective
maximum stiffness, quality), the design space (``DesignSpaceBuilder``: body and material)
and the mass target (``OptimizationConstraintBuilder``, ``MassTarget`` in kg) inside one
Topology Optimization task. Supports, loads, keep-in/out regions and shape constraints
follow with ``nx_create_topology_condition``. Every builder is destroyed; a failure
cancels the task and the kernel rolls the call back.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Doc, Fraction, Name, Range, Text
from nx_mcp.domains.optimization import common
from nx_mcp.kit import builders, readback, result, select

MASS_REL = 1e-6
MATERIAL_CODES = {3940020: "NX_NOT_FOUND"}  # no such material in the library


class StudyData(TypedDict):
    study: result.Row
    name: str
    design_space: result.Row
    material: str | None
    design_mass: float
    target_mass: float
    mass_fraction: float
    quality: int


def _own_material(tx, body):
    """The body's assigned material (``AskMaterialOfObject``; NX 2406 has no
    ``GetUsedMaterial``), else ``NX_INVALID_ARGUMENT`` before the task is entered (NX
    would refuse the design space: "Material must be assigned to design space body")."""
    try:
        own = tx.part.MaterialManager.PhysicalMaterials.AskMaterialOfObject(body)
    except Exception:  # NX raises for a body without a material
        own = None
    if own is None:
        fail("NX_INVALID_ARGUMENT", "The design space body has no material", field="/material",
             hint="Pass material (a library name) or assign one with nx_set_material")  # fmt: skip
    return own


def _library_material(tx, name):
    """The library material ``name``, loaded into the part."""
    materials = tx.part.MaterialManager.PhysicalMaterials
    with native(MATERIAL_CODES):
        loaded = materials.GetLoadedLibraryMaterial("", name)
        if loaded is None:
            loaded = materials.LoadFromNxmatmllibrary(name)
    return loaded


def _summary(a, say):
    fraction = a.get("mass_fraction")
    goal = f" to {float(fraction) * 100:g}% mass" if isinstance(fraction, (int, float)) else ""
    return f"Topology study {say('design_space')}{goal}"


@tool(
    effect="model",
    label="Create topology study",
    summary=_summary,
    keywords=(
        "topology optimization",
        "lightweight",
        "design space",
        "generative design",
        "optimize bracket",
        "mass reduction",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    display_bound=True,
    example={"design_space": "b:3", "mass_fraction": 0.4, "material": "Aluminum_6061"},
)
def nx_create_topology_study(
    tx: Tx,
    *,
    design_space: BodyRef,
    mass_fraction: Annotated[Fraction, Range(gt=0, lt=1), Doc("Target of design mass; 0.4")],
    material: Annotated[str, Text(80), Doc("Library name; default the body's")] | None = None,
    quality: Annotated[int, Range(ge=1, le=9), Doc("1 fine..9 coarse; NX 5")] | None = None,
    name: Name | None = None,
) -> StudyData:
    """Create a topology optimization study (max stiffness at a mass fraction) on a solid.
    Lighter brackets and mounts: add supports, loads and regions with
    nx_create_topology_condition, then nx_run_topology_study. License des_top_opt."""
    with tx.checks():
        body = select.one(tx, design_space, "body", field="/design_space")
        if not getattr(body, "IsSolidBody", True):
            fail("NX_WRONG_GEOMETRY", "The design space must be a solid", field="/design_space")
        common.require_license(tx)
        own = _own_material(tx, body) if material is None else None
    DS = common.ds(tx)
    with common.task(tx) as feature:
        with builders.build(tx, feature.CreateStudyBuilder, DS.Study.Null) as b:
            if name:
                b.StudyName = name
            if quality is not None:
                b.StudyQuality = int(quality)
            b.OptimizationObjectiveType = (
                DS.StudyBuilder.OptimizationObjectiveOption.MaximumStiffness
            )
            study = b.commit()
            if study is None:
                study = feature.ActiveStudy
        chosen = own if material is None else _library_material(tx, material)
        with builders.build(tx, study.CreateDesignSpaceBuilder, DS.DesignSpace.Null) as b:
            b.SelectBody.Value = body
            if chosen is not None:
                b.Material = chosen
            design = b.commit()
            if design is None:
                design = common.all_of(study, "GetAllDesignSpaces")[-1]
        design_mass = common.mass_of(design)
        if not design_mass:
            fail("NX_INCOMPLETE_RESULT", "NX reports no design space mass", field="/design_space")
        target = float(mass_fraction) * design_mass
        with builders.build(
            tx, study.CreateOptimizationConstraintBuilder, DS.OptimizationConstraint.Null
        ) as b:
            b.DesignSpace = design
            b.Type = DS.OptimizationConstraintBuilder.ConstraintType.MassTarget
            common.drive(tx, b.Mass, target, "mass")
            b.commit()
    study = common.active_study(feature)
    designs = common.all_of(study, "GetAllDesignSpaces")
    readback.expect(len(designs) == 1, "design_spaces", count=len(designs))
    readback.expect(int(designs[0].Body.Tag) == int(body.Tag), "design_space")
    got = common.target_mass(feature)
    readback.expect_close(got, target, "target_mass", tol=MASS_REL * target)
    if name:
        readback.expect(str(study.Name) == name, "name", name=str(study.Name))
    material_of = getattr(designs[0], "Material", None) or chosen
    return {
        "study": tx.ref(feature, kind="feature"),
        "name": str(getattr(study, "Name", "") or ""),
        "design_space": tx.ref(body, kind="body"),
        "material": None if material_of is None else str(material_of.Name),
        "design_mass": design_mass,
        "target_mass": float(got) if got is not None else target,
        "mass_fraction": float(mass_fraction),
        "quality": int(study.StudyQuality),
    }

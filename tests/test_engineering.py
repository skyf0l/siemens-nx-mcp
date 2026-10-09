"""Regression contracts for observed NX failures; native receipts validate geometry.

These seams verify validation, result cardinality, transform semantics, and cleanup.
They do not claim to emulate the NX geometric kernel.
"""

import contextlib
import struct
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.runtime import NXToolError
from tests.fakes import Body, Feature, Object, Sketch, point
from tests.kit.objects import Collector

pytestmark = pytest.mark.fake_nx


@pytest.fixture
def eng(rig, monkeypatch):
    r = rig
    r.body = Body()
    r.feature = Feature(bodies=[r.body])
    r.sketch = Sketch(r.session)
    monkeypatch.setattr(Sketch, "Feature", r.feature, raising=False)
    r.part.Bodies.append(r.body)
    r.part.Features.append(r.feature)
    r.part.Sketches.append(r.sketch)
    for o in [r.body, r.feature, r.sketch, *r.body.faces, *r.body.edges]:
        o.OwningPart = r.part
        o.IsOccurrence = False
    for o in [*r.body.faces, *r.body.edges]:
        o.GetBody = lambda: r.body
    r.nx.SmartObject = NS(UpdateOption=NS(WithinModeling=1))
    r.nx.Features.MoveObject = Feature
    r.part.Directions = NS(CreateDirection=lambda p, v, _: NS(origin=p, vector=v))
    r.part.CoordinateSystems = NS(
        CreateCoordinateSystem=lambda p, m, _: NS(Origin=p, Orientation=m)
    )
    r.part.ScRuleFactory = NS(CreateRuleOptions=lambda: NS(Dispose=Mock()))
    for kind in ["Edge", "Body", "Face"]:
        setattr(r.part.ScRuleFactory, "CreateRule" + kind + "Dumb", lambda objects: objects)
    r.part.ScRuleFactory.CreateRuleCurveFeature = lambda objects, *_: objects

    r.part.ScCollectors = NS(CreateCollector=Collector)
    r.part.Sections = NS(CreateSection=lambda: NS(AddToSection=Mock()))
    r.nx.Section = NS(Mode=NS(Create=1))
    g = NS(
        Extend=NS(ExtendType=NS(Value="value", ThroughAll="all", UntilSelected="face")),
        BooleanOperation=NS(
            BooleanType=NS(
                Create="create", Unite="unite", Subtract="subtract", Intersect="intersect"
            )
        ),
    )
    monkeypatch.setitem(sys.modules, "NXOpen.GeometricUtilities", g)
    r.nx.GeometricUtilities = g
    r.e._update_model = Mock()
    r.e._check_sketch_result = Mock(return_value={"valid": True})
    r.e._editing_sketch = lambda _: contextlib.nullcontext()
    r.builders = []

    def builder(*_):
        def expression():
            return NS(RightHandSide="0")

        b = NS(
            Destroy=Mock(),
            CommitFeature=Mock(return_value=r.feature),
            Commit=Mock(return_value=r.feature),
            DefaultThickness=expression(),
            StationaryReference=Collector(),
            FaceSetAngleExpressionList=NS(Append=Mock()),
            Type=NS(Face=1),
            SectionsList=NS(Append=Mock()),
            BodyPreferenceTypes=NS(Solid=1, Sheet=2),
            Limits=NS(StartExtend=NS(Value=expression()), EndExtend=NS(Value=expression())),
            BooleanOperation=NS(SetTargetBodies=Mock()),
            BooleanOption=NS(SetTargetBodies=Mock()),
            Diameter=expression(),
            Height=expression(),
            FirstOffsetExp=expression(),
            ChamferOption=NS(SymmetricOffsets=1),
            AddChainset=Mock(),
            ExtractType=NS(Body=1),
            ExtractBodyCollector=Collector(),
            ObjectToMoveObject=NS(Add=Mock()),
            MoveObjectResultOptions=NS(MoveOriginal=1),
            TransformMotion=NS(Options=NS(CsysToCsys=1)),
            MirrorBodyCollector=Collector(),
            Plane=NS(),
            SectionList=NS(Append=Mock()),
            GuideList=NS(Append=Mock()),
        )
        r.builders.append(b)
        return b

    r.builder = builder
    for name in [
        "Extrude",
        "Shell",
        "ThroughCurves",
        "Draft",
        "EdgeBlend",
        "Chamfer",
        "Cylinder",
        "ExtractFace",
        "MirrorBody",
        "Swept",
    ]:
        setattr(r.part.Features, "Create" + name + "Builder", builder)
    r.part.BaseFeatures = NS(CreateMoveObjectBuilder=builder)
    r.part.Datums = NS(CreateFixedDatumPlane=Mock(return_value=Object("plane")))
    r.part.CreateExpressionCollectorSet = lambda *args: args
    return r


def test_owned_geometry_rejects_occurrences_and_other_parts(eng):
    ref = eng.ref(eng.body)
    eng.body.IsOccurrence = True
    with pytest.raises(NXToolError, match="owned"):
        eng.e._engineering_owned(ref, "body")
    eng.body.IsOccurrence = False
    eng.body.OwningPart = object()
    with pytest.raises(NXToolError, match="owned"):
        eng.e._engineering_owned(ref, "body")


@pytest.fixture
def rendering(eng):
    r = eng
    r.nx.View.RenderingStyleType.Studio = "studio"
    view = NS(RenderingStyle="original", UpdateDisplay=Mock())
    r.part.ModelingViews = NS(WorkView=view)
    r.e._view_info = lambda: {"camera": "unchanged"}
    lights = NS(
        LightsShadedViewsLightingCollection="old",
        LightingCollectionType=NS(**{"Lighting" + str(i): i for i in range(1, 6)}),
        Commit=Mock(),
        Destroy=Mock(),
    )
    capture = NS(
        SourceType=NS(WorkView=1),
        UnitsEnumType=NS(Pixels=1),
        BackgroundOptions=NS(Original=1, CustomColor=2, Transparent=3),
        SetCustomBackgroundColor=Mock(),
        Destroy=Mock(),
    )

    def dimensions(v):
        capture.dimensions = v

    capture.SetImageDimensionsInteger = dimensions

    def commit():
        height, width = capture.dimensions
        data = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", width, height)
        Path(capture.NativeFileBrowser).write_bytes(data)

    capture.Commit = Mock(side_effect=commit)
    r.part.Views = NS(
        CreateStudioImageCaptureBuilder=lambda: capture, CreateLighting=lambda _: lights
    )
    r.view, r.lights, r.capture = view, lights, capture
    return r


@pytest.mark.parametrize(
    "background,color",
    [("white", None), ("original", None), ("transparent", None), ("color", [0.2, 0.3, 0.4])],
)
def test_render_artifact_dimensions_checksum_and_view_restoration(rendering, background, color):
    import hashlib

    r = rendering
    out = r.e._render_view(width=640, height=480, background=background, color=color, lighting=2)
    assert out["resolution"] == [640, 480]
    assert out["sha256"] == hashlib.sha256(Path(out["path"]).read_bytes()).hexdigest()
    assert r.view.RenderingStyle == "original"
    assert r.lights.LightsShadedViewsLightingCollection == "old"
    r.lights.Destroy.assert_called_once()
    r.capture.Destroy.assert_called_once()
    with pytest.raises(NXToolError):
        r.e._render_view(path=out["path"])


@pytest.mark.parametrize(
    "params",
    [
        {"width": True},
        {"height": 100},
        {"background": "bad"},
        {"style": "bad"},
        {"color": [1, 1, 1]},
        {"background": "color"},
        {"background": "color", "color": [0, 0, 2]},
        {"lighting": 0},
        {"lighting": True},
        {"path": "bad.jpg"},
    ],
)
def test_invalid_render_requests_have_no_side_effects(rendering, params):
    if "path" in params:
        params = {**params, "path": str(rendering.e.workspace.root / params["path"])}
    with pytest.raises(NXToolError):
        rendering.e._render_view(**params)
    rendering.capture.Commit.assert_not_called()
    assert rendering.view.RenderingStyle == "original"


@pytest.mark.parametrize("failure", ["native", "invalid_png", "wrong_resolution"])
def test_render_failure_removes_partial_file_and_restores_view(rendering, failure):
    r = rendering
    file = r.e.workspace.root / "bad.png"

    def commit():
        file.write_bytes(b"partial")
        if failure == "native":
            raise RuntimeError("failed")
        if failure == "wrong_resolution":
            file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 1, 1))

    r.capture.Commit.side_effect = commit
    with pytest.raises((RuntimeError, NXToolError)):
        r.e._render_view(path=str(file), lighting=1)
    assert not file.exists() and r.view.RenderingStyle == "original"
    assert r.lights.LightsShadedViewsLightingCollection == "old"


def test_render_requires_interactive_display_part(rendering):
    r = rendering
    r.session.IsBatch = True
    with pytest.raises(NXToolError, match="interactive"):
        r.e._render_view()
    r.session.IsBatch = False
    r.session.Parts.Display = None
    with pytest.raises(NXToolError, match="display part"):
        r.e._render_view()


@pytest.fixture
def material(eng):
    r = eng
    r.nx.PhysicalMaterial = NS(Type=NS(Isotropic=1))
    materials = []

    class Materials(list):
        def CreatePhysicalMaterialBuilder(self, _):
            b = NS(
                PropertyTable=NS(
                    SetBaseScalarWithDataPropertyValue=lambda key, value, unit: setattr(
                        r, "density", value
                    )
                ),
                Destroy=Mock(),
            )

            def commit():
                m = NS(Name=b.Name, AssignObjects=Mock())
                self.append(m)
                return m

            b.Commit = commit
            r.material_builder = b
            return b

        def AskMaterialOfObject(self, _):
            return self[0] if self else None

    r.materials = Materials(materials)
    r.part.MaterialManager = NS(PhysicalMaterials=r.materials)
    r.part.UnitCollection = NS(FindObject=lambda name: name)
    r.nx.UF.Modl = NS(DensityUnits=NS(KILOGRAMS_METERS=1))
    r.density = 1000
    r.uf.Modeling = NS(AskBodyDensity=lambda *_: r.density)
    return r


def test_mass_tensor_uses_centroidal_values_and_product_signs(material):
    from tests.fakes import matrix

    r = material
    values = list(range(47))
    r.uf.Modeling.AskMassProps3d = Mock(return_value=(values, [0] * 13))
    r.part.WCS = NS(CoordinateSystem=NS(Origin=point(1, 2, 3), Orientation=NS(Element=matrix())))
    out = r.e._mass_properties()
    assert out["center_of_gravity_m"] == [3, 4, 5]
    assert out["inertia_tensor_centroid_kg_m2"] == [[12, -19, -21], [-19, 13, -20], [-21, -20, 14]]
    assert out["wcs_origin_in_part_units"] == [1, 2, 3]
    assert out["semantics"] == "sum_of_included_bodies_with_assigned_densities"


@pytest.fixture
def assembly_constraints(eng, monkeypatch):
    from tests.fakes import Component, Face

    r = eng

    class Constraint(Object):
        Type = NS(Fix=1, Distance=2, Angle=3, Touch=4, Parallel=5, Perpendicular=6, Concentric=7)
        Alignment = NS(InferAlign=1, CoAlign=2, ContraAlign=3)
        SolverStatus = NS(Solved=1, NotSolved=2)

        def __init__(self):
            super().__init__("constraint")
            self.Suppressed = False
            self.Expression = 5
            self.refs = []
            self.status = 1

        def CreateConstraintReference(self, obj, geom, *_):
            self.refs.append(NS(GetMovableObject=lambda: obj, GetGeometry=lambda: geom))

        def SetExpression(self, value):
            self.Expression = float(value)

        def GetConstraintStatus(self):
            return self.status

        def GetReferences(self):
            return self.refs

    module = NS(Constraint=Constraint)
    monkeypatch.setitem(sys.modules, "NXOpen.Positioning", module)
    r.nx.Positioning = module
    r.nx.Assemblies = NS(Component=Component)
    root = Component("root")
    moving = Component("moving", parent=root)
    target = Component("target", parent=root)
    root.children = [moving, target]
    r.part.ComponentAssembly.RootComponent = root
    faces = []
    for c in [moving, target]:
        f = Face()
        f.IsOccurrence = True
        f.OwningPart = r.part
        f.OwningComponent = c
        faces.append(f)
    pos = NS(
        Constraints=[],
        BeginAssemblyConstraints=Mock(),
        EndAssemblyConstraints=Mock(),
        ClearNetwork=Mock(),
    )

    def create(_):
        c = Constraint()
        pos.Constraints.append(c)
        return c

    pos.CreateConstraint = create
    networks = []

    def network():
        captured = {c.Tag: c.Expression for c in pos.Constraints}
        net = NS(AddConstraint=Mock(), Solve=Mock(), ApplyToModel=Mock(), captured=captured)
        networks.append(net)
        return net

    pos.EstablishNetwork = network
    r.part.ComponentAssembly.Positioner = pos
    r.e._expression_record = lambda e: {"value": e}
    r.pos, r.networks, r.Constraint = pos, networks, Constraint
    r.moving, r.target, r.faces = moving, target, faces
    return r


def test_assembly_distance_edit_builds_network_after_changing_expression(assembly_constraints):
    r = assembly_constraints
    result = r.e._assembly_constraint(
        "distance",
        r.ref(r.moving, "component"),
        r.ref(r.faces[0], "face"),
        r.ref(r.target, "component"),
        r.ref(r.faces[1], "face"),
        5,
        "opposite",
    )
    ref = result["object"]["id"]
    constraint = r.pos.Constraints[0]
    result = r.e._edit_assembly_constraint(ref, value=12, alignment="same")
    assert r.networks[-1].captured[constraint.Tag] == 12, "Network captured the old expression"
    assert result["expression"]["value"] == 12 and result["alignment"] == "CoAlign"
    r.e._edit_assembly_constraint(ref, suppressed=True)
    assert r.e._list_assembly_constraints()["constraints"][0]["suppressed"]
    assert r.pos.ClearNetwork.call_count == 3 and r.pos.EndAssemblyConstraints.call_count == 3


def test_assembly_fix_and_solver_failure_cleanup(assembly_constraints):
    r = assembly_constraints
    result = r.e._assembly_constraint("fix", r.ref(r.moving, "component"))
    constraint = r.pos.Constraints[0]
    constraint.status = 2
    with pytest.raises(NXToolError, match="not solved"):
        r.e._edit_assembly_constraint(result["object"]["id"], suppressed=False)
    assert r.pos.ClearNetwork.call_count == 2 and r.pos.EndAssemblyConstraints.call_count == 2
    with pytest.raises(NXToolError):
        r.e._edit_assembly_constraint(result["object"]["id"], value=12)
    with pytest.raises(NXToolError):
        r.e._edit_assembly_constraint(result["object"]["id"])


@pytest.mark.parametrize(
    "change",
    [
        "same_component",
        "wrong_owner",
        "bad_type",
        "bad_alignment",
        "missing_value",
        "negative",
        "fix_extra",
        "suppressed",
    ],
)
def test_invalid_assembly_constraints_do_not_create_network(assembly_constraints, change):
    r = assembly_constraints
    args = {
        "constraint_type": "distance",
        "component": r.ref(r.moving, "component"),
        "geometry": r.ref(r.faces[0], "face"),
        "target_component": r.ref(r.target, "component"),
        "target_geometry": r.ref(r.faces[1], "face"),
        "value": 5,
    }
    if change == "same_component":
        args["target_component"] = args["component"]
    if change == "wrong_owner":
        args["geometry"] = args["target_geometry"]
    if change == "bad_type":
        args["constraint_type"] = "bogus"
    if change == "bad_alignment":
        args["alignment"] = "bogus"
    if change == "missing_value":
        args["value"] = None
    if change == "negative":
        args["value"] = -5
    if change == "fix_extra":
        args["constraint_type"] = "fix"
    if change == "suppressed":
        r.moving.IsSuppressed = True
    with pytest.raises(NXToolError):
        r.e._assembly_constraint(**args)
    r.pos.BeginAssemblyConstraints.assert_not_called()


@pytest.mark.parametrize("handle", ["capture", "lights"])
def test_render_cleanup_failure_still_restores_style_and_removes_artifact(rendering, handle):
    r = rendering
    getattr(r, handle).Destroy.side_effect = RuntimeError("cleanup failed")
    path = r.e.workspace.root / "cleanup.png"
    with pytest.raises(NXToolError) as exc:
        r.e._render_view(path=str(path), lighting=1)
    assert exc.value.details["mutation_outcome"] == "partial"
    assert r.view.RenderingStyle == "original" and not path.exists()
    r.lights.Destroy.assert_called_once()

from pathlib import Path
from types import SimpleNamespace

import pytest

from nx_mcp.nx_bridge import NXOpenExecutor, start_bridge, stop_bridge
from nx_mcp.runtime import NXToolError
from nx_mcp.workspace import Workspace

pytestmark = [pytest.mark.integration, pytest.mark.fake_nx]


class FakeObject:
    def __init__(self, tag: int, name: str):
        self.Tag = tag
        self.Name = name

    def SetName(self, name):
        self.Name = name


class FakeSketch(FakeObject):
    def __init__(self, tag: int, name: str):
        super().__init__(tag, name)
        self.Feature = self
        self.geometry = []
        self.active = False

    def Activate(self, reorient):
        self.active = True

    def AddGeometry(self, geometry, infer_option):
        self.geometry.append(geometry)

    def Deactivate(self, reorient, update_level):
        self.active = False


class FakeCollection:
    def __init__(self, *values):
        self.values = list(values)

    def __iter__(self):
        return iter(self.values)


class FakeSketchBuilder:
    def __init__(self, sketches):
        self.sketches = sketches

    def Commit(self):
        sketch = FakeSketch(102 + len(self.sketches.values), "SKETCH(2)")
        self.sketches.values.append(sketch)
        return sketch

    def Destroy(self):
        pass


class FakeSketches(FakeCollection):
    def CreateSketchInPlaceBuilder2(self, operation):
        self.last_builder = FakeSketchBuilder(self)
        return self.last_builder


class FakePlanes:
    def CreatePlane(self, origin, normal, update_option):
        self.last_plane = SimpleNamespace(
            origin=origin,
            normal=normal,
            update_option=update_option,
        )
        return self.last_plane


class FakeCurves:
    def __init__(self):
        self.values = []

    def CreateLine(self, start, end):
        line = FakeObject(400 + len(self.values), f"LINE({len(self.values) + 1})")
        line.start = start
        line.end = end
        self.values.append(line)
        return line


class FakeFeature(FakeObject):
    def __init__(self, tag: int, name: str, bodies):
        super().__init__(tag, name)
        self._bodies = bodies

    def GetBodies(self):
        return list(self._bodies)


class FakeExtrudeBuilder:
    def __init__(self, part):
        self.part = part
        self.Limits = SimpleNamespace(
            StartExtend=SimpleNamespace(Value=SimpleNamespace(RightHandSide=None)),
            EndExtend=SimpleNamespace(Value=SimpleNamespace(RightHandSide=None)),
        )
        self.BooleanOperation = SimpleNamespace(Type=None)

    def AllowSelfIntersectingSection(self, allowed):
        self.allow_self_intersecting_section = allowed

    def CommitFeature(self):
        body = FakeObject(202 + len(self.part.Bodies.values), "BODY(2)")
        feature = FakeFeature(302 + len(self.part.Features.values), "EXTRUDE(2)", [body])
        self.part.Bodies.values.append(body)
        self.part.Features.values.append(feature)
        return feature

    def Destroy(self):
        pass


class FakeFeatures(FakeCollection):
    def __init__(self, part, *values):
        super().__init__(*values)
        self.part = part

    def CreateExtrudeBuilder(self, feature):
        self.last_extrude_builder = FakeExtrudeBuilder(self.part)
        return self.last_extrude_builder


class FakeSection:
    def AddToSection(
        self,
        rules,
        first,
        second,
        third,
        point,
        mode,
        chain,
    ):
        self.rules = rules
        self.point = point
        self.mode = mode
        self.chain = chain


class FakeSections:
    def CreateSection(self):
        self.last_section = FakeSection()
        return self.last_section


class FakeRuleFactory:
    def CreateRuleOptions(self):
        return SimpleNamespace()

    def CreateRuleCurveFeature(self, features, displayable, options):
        return SimpleNamespace(features=features)


class FakeDirections:
    def CreateDirection(self, sketch, sense, update_option):
        self.last_direction = SimpleNamespace(
            sketch=sketch,
            sense=sense,
            update_option=update_option,
        )
        return self.last_direction


class FakePart(FakeObject):
    def __init__(self, name: str = "bracket", tag: int = 42):
        super().__init__(tag, name)
        self.FullPath = f"C:/workspace/{name}.prt"
        self.Sketches = FakeSketches(FakeSketch(101, "SKETCH(1)"))
        self.Bodies = FakeCollection(FakeObject(201, "BODY(1)"))
        self.Features = FakeFeatures(self, FakeFeature(301, "EXTRUDE(1)", [self.Bodies.values[0]]))
        self.Planes = FakePlanes()
        self.Sections = FakeSections()
        self.ScRuleFactory = FakeRuleFactory()
        self.Directions = FakeDirections()
        self.Curves = FakeCurves()
        self.ModelingViews = SimpleNamespace(WorkView=SimpleNamespace(Fit=lambda: None))
        self.saved = False
        self._parts = None

    def Save(self, save_components, close_after_save):
        self.saved = True

    def Close(self, whole_tree, close_modified, responses):
        self.close_args = (whole_tree, close_modified, responses)
        self._parts.Work = None
        self._parts.Display = None
        self._parts.last_closed = self


class FakeLoadStatus:
    def Dispose(self):
        pass


class FakeStepCreator:
    def __init__(self):
        self.ObjectTypes = SimpleNamespace()

    def Commit(self):
        assert self.ExportFrom == "existing-part"
        assert self.ObjectTypes.Solids is True
        Path(self.OutputFile).write_text(
            "ISO-10303-21;\nMANIFOLD_SOLID_BREP();\nEND-ISO-10303-21;", encoding="utf-8"
        )

    def Destroy(self):
        pass


class FakeDexManager:
    def CreateStepCreator(self):
        return FakeStepCreator()


class FakeFileNew:
    def __init__(self, parts):
        self.parts = parts

    def Commit(self):
        part = FakePart(Path(self.NewFileName).stem, 43)
        part.FullPath = self.NewFileName
        self.parts._set_active(part)
        return part

    def Destroy(self):
        pass


class FakeParts:
    def __init__(self):
        self._set_active(FakePart())

    def _set_active(self, part):
        part._parts = self
        self.Work = part
        self.Display = part

    def __iter__(self):
        return iter([self.Work])

    def FileNew(self):
        self.last_file_new = FakeFileNew(self)
        return self.last_file_new

    def OpenBaseDisplay(self, path):
        part = FakePart(Path(path).stem, 44)
        part.FullPath = path
        self._set_active(part)
        return part, FakeLoadStatus()


class FakeSession:
    def __init__(self):
        self.Parts = FakeParts()
        self.DexManager = FakeDexManager()
        self.undo_to_marks = []
        self.rollback_count = 0
        self.next_mark = 1
        self.marks = {}

    def SetUndoMark(self, visibility, name):
        mark = self.next_mark
        self.next_mark += 1
        self.marks[mark] = name
        return mark

    def UndoToMark(self, mark, name):
        self.undo_to_marks.append(mark)
        self.rollback_count += 1
        part = self.Parts.Work
        if self.marks.get(mark) == "NX MCP: nx_extrude" and len(part.Bodies.values) > 1:
            part.Bodies.values.pop()
            part.Features.values.pop()


FAKE_NXOPEN = SimpleNamespace(
    StepCreator=SimpleNamespace(ExportFromOption=SimpleNamespace(ExistingPart="existing-part")),
    BasePart=SimpleNamespace(
        SaveComponents=SimpleNamespace(TrueValue=True, FalseValue=False),
        CloseAfterSave=SimpleNamespace(FalseValue=False),
        CloseWholeTree=SimpleNamespace(TrueValue="whole-tree"),
        CloseModified=SimpleNamespace(CloseModified="close"),
    ),
    Part=SimpleNamespace(Units=SimpleNamespace(Millimeters="mm", Inches="inch")),
    DisplayPartOption=SimpleNamespace(AllowAdditional="allow-additional"),
    DisplayableObject=SimpleNamespace(Null=None),
    NXObject=SimpleNamespace(Null=None),
    Vector3d=lambda x, y, z: (x, y, z),
    Point3d=lambda x, y, z: (x, y, z),
    Sketch=SimpleNamespace(
        Null=None,
        ViewReorient=SimpleNamespace(TrueValue=True),
        UpdateLevel=SimpleNamespace(Model="model"),
        InferConstraintsOption=SimpleNamespace(InferNoConstraints="none"),
    ),
    SmartObject=SimpleNamespace(UpdateOption=SimpleNamespace(WithinModeling="within-modeling")),
    Section=SimpleNamespace(Mode=SimpleNamespace(Create="create")),
    Sense=SimpleNamespace(Forward="forward", Reverse="reverse"),
    Features=SimpleNamespace(Feature=SimpleNamespace(Null=None)),
    GeometricUtilities=SimpleNamespace(
        BooleanOperation=SimpleNamespace(BooleanType=SimpleNamespace(Create="create"))
    ),
    Expression=SimpleNamespace(ValueType=SimpleNamespace(Double="double")),
    Unit=SimpleNamespace(CollectionType=SimpleNamespace(Millimeter="mm")),
    Session=SimpleNamespace(MarkVisibility=SimpleNamespace(Visible="visible")),
)


def test_failed_model_mutation_rolls_back_its_undo_mark(tmp_path: Path):
    session = FakeSession()
    executor = NXOpenExecutor(session, FAKE_NXOPEN, "NX test", Workspace(tmp_path))

    def refuse(**_kw):
        raise NXToolError("NX_INVALID_ARGUMENT", "refused after the mark was set")

    executor._MODEL_MUTATIONS = {"nx_test_mutation"}
    executor._handlers["nx_test_mutation"] = refuse
    with pytest.raises(NXToolError):
        executor.execute("nx_test_mutation", {})

    assert session.rollback_count == 1


def test_python_nx_bridge_requires_explicit_feasibility_gate(tmp_path: Path, monkeypatch):
    stop_bridge()
    monkeypatch.delenv("NX_MCP_ALLOW_UNVERIFIED_PYTHON_BRIDGE", raising=False)

    with pytest.raises(RuntimeError, match="has not been verified"):
        start_bridge(tmp_path)

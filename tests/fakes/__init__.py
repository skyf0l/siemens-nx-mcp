"""Stateful NX seams for failure/recovery tests; not a geometric kernel oracle.

The fake NX objects live in ``tests.kit.objects`` (the shared fake kit); the
``rig`` fixture wires them to the legacy ``HardenedExecutor``.
"""

from types import ModuleType
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.hardened import HardenedExecutor
from nx_mcp.workspace import Workspace
from tests.kit.objects import (  # noqa: F401  (re-exported for legacy tests)
    Body,
    Collection,
    Component,
    Curve,
    Edge,
    Face,
    Feature,
    Object,
    Part,
    Session,
    Sketch,
    matrix,
    point,
)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    session = Session()
    part = Part(session, tmp_path / "test.prt")
    nx = ModuleType("NXOpen")
    nx.Body = Body
    nx.Face = Face
    nx.Edge = Edge
    nx.Sketch = Sketch
    nx.Point3d = point
    nx.Vector3d = point
    nx.Matrix3x3 = matrix
    nx.Features = NS(Feature=Feature)
    nx.Session = NS(MarkVisibility=NS(Visible=1, Invisible=0))
    nx.MeasureBodies = NS(AnalysisUnit=NS(KilogramMillimeter=6))
    nx.BasePart = NS(
        Units=NS(Millimeters="mm"),
        SaveComponents=NS(TrueValue=True, FalseValue=False),
        CloseAfterSave=NS(FalseValue=False),
        CloseWholeTree=NS(FalseValue=False),
        CloseModified=NS(CloseModified=0),
    )
    nx.SketchWorkRegionBuilder = NS(ScopeType=NS(EntireSketch="all"))
    nx.View = NS(
        RenderingStyleType=NS(Shaded="shaded", ShadedWithEdges="edges", StaticWireframe="wire")
    )
    session.Parts.SetWork = lambda p: setattr(session.Parts, "Work", p)

    def display(p, *_):
        session.Parts.Display = p
        return None, NS(Dispose=Mock())

    session.Parts.SetDisplay = display
    session.Parts.OpenBase = lambda path: (
        Part(session, __import__("pathlib").Path(path)),
        NS(Dispose=Mock()),
    )
    e = HardenedExecutor(session, nx, "v2606 fake seam", Workspace(tmp_path))

    def ref(obj, kind="body"):
        return e._reference(obj, kind, part, kind)["id"]

    def object_for(tag):
        return next(entry.value for entry in e.objects._objects.values() if entry.value.Tag == tag)

    uf = NS(
        Obj=NS(AskTranslucency=lambda tag: object_for(tag).transparency),
        Disp=NS(
            ColorName=NS(RED_NAME=3, MEDIUM_GRAY_NAME=5), AskClosestColorInDisplayedPart=lambda x: x
        ),
        ModlGeneral=NS(
            AskBoundingBox=lambda tag: object_for(tag).box,
            AskBoundingBoxExact=lambda tag, _: (
                object_for(tag).box[:3],
                [1, 0, 0, 0, 1, 0, 0, 0, 1],
                [object_for(tag).box[i + 3] - object_for(tag).box[i] for i in range(3)],
            ),
        ),
    )
    modules = {
        "Preferences": NS(
            Font=NS(Solid=1, Dashed=2, ValueOf=lambda value: value),
            Width=NS(
                Original=0,
                Thin=1,
                Normal=2,
                Thick=3,
                One=5,
                Two=6,
                Three=7,
                Four=8,
                Five=9,
                Six=10,
                Seven=11,
                Eight=12,
                Nine=13,
            ),
            ShadingRenderingStyleOption=NS(FullyShaded=0, PartiallyShaded=1, Wireframe=2),
        ),
        "UF": NS(UFSession=NS(GetUFSession=lambda: uf)),
        "Display": NS(
            DynamicSectionTypes=NS(
                Type=NS(OnePlane=1), CoordinateSystem=NS(Absolute=1), Clip=NS(Section=1)
            )
        ),
        "Gateway": NS(
            ImageExportBuilder=NS(
                FileFormats=NS(Png=1),
                BackgroundOptions=NS(CustomColor=1, Original=2, Transparent=3),
            )
        ),
        "GeometricAnalysis": NS(
            SimpleInterference=NS(
                InterferenceMethod=NS(InterferenceSolid=1),
                Result=NS(InterferenceExists=1, OnlyEdgesOrFacesInterfere=2, NoInterference=3),
            )
        ),
    }
    monkeypatch.setitem(__import__("sys").modules, "NXOpen", nx)
    for name, mod in modules.items():
        setattr(nx, name, mod)
        monkeypatch.setitem(__import__("sys").modules, "NXOpen." + name, mod)
    modifications = []

    def modification():
        m = NS(Dispose=Mock())

        def apply(values):
            for obj in values:
                for target in [obj] + (obj.GetFaces() if isinstance(obj, Body) else []):
                    if hasattr(m, "NewColor"):
                        target.Color = m.NewColor
                    if hasattr(m, "NewTranslucency"):
                        target.transparency = m.NewTranslucency

        m.Apply = Mock(side_effect=apply)
        modifications.append(m)
        return m

    session.DisplayManager = NS(NewDisplayModification=modification)
    # The test part stands for a part the agent created (part-ownership gate 6);
    # ownership tests revoke it with services_for(e).x._owned_parts.clear().
    from nx_mcp.host.dispatch import services_for

    services_for(e).own(part)
    return NS(e=e, session=session, part=part, nx=nx, uf=uf, ref=ref, modifications=modifications)

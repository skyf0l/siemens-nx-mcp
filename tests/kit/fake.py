"""``fake_nx(units)``: one fake NXOpen session for kit and domain unit tests.

It is a seam, not a kernel: builders record what was configured, committed
and destroyed; geometry is whatever the test declares (``block``, ``face``,
``datum_plane``, ``sketch``, ``sheet``...). ``nx.tx`` is a ``Tx``-shaped object
(``nx``, ``uf``, ``session``, ``part``, ``resolve``, ``ref``) for calling kit
helpers directly.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from types import SimpleNamespace as NS

from nx_mcp.core.errors import fail
from tests.kit.objects import (
    Body,
    Collection,
    Collector,
    Edge,
    Expression,
    Face,
    Feature,
    NXException,
    Object,
    Part,
    Session,
    Sketch,
    matrix,
    point,
)

FACE_TYPES = {"plane": 22, "cylinder": 16, "cone": 17, "sphere": 18}
UNIT_ENUM = NS(Millimeters="mm", Inches="in")


# ---- builders -----------------------------------------------------------------------
class Node:
    """An auto-vivifying builder member: unknown attributes are child nodes, calls
    are recorded and return a child node. Assigned values are kept as is."""

    def __init__(self, path):
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "calls", [])

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        child = Node(f"{self._path}.{name}")
        object.__setattr__(self, name, child)
        return child

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return Node(f"{self._path}()")

    def __repr__(self):
        return f"<Node {self._path}>"


class FakeBuilder(Node):
    """A native builder: records configure/commit/Destroy.

    ``missing`` names attributes this NX build lacks (``hasattr`` is False).
    ``error`` (an exception) is raised by Commit/CommitFeature.
    """

    def __init__(self, fake, kind, args, missing=(), error=None, feature=True):
        super().__init__(kind)
        for name, value in {
            "fake": fake,
            "kind": kind,
            "args": args,
            "missing": set(missing),
            "error": error,
            "commits": 0,
            "destroyed": False,
            "valid": True,
            "result": None,
        }.items():
            object.__setattr__(self, name, value)
        if not feature:
            self.missing.add("CommitFeature")

    def __getattribute__(self, name):
        if not name.startswith("_") and name in object.__getattribute__(self, "__dict__").get(
            "missing", ()
        ):
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    def __getattr__(self, name):  # reached for unknown and for missing members
        if name in self.__dict__.get("missing", ()):
            raise AttributeError(name)
        return super().__getattr__(name)

    def Validate(self):
        return self.valid

    def _commit(self):
        if self.error is not None:
            raise self.error
        object.__setattr__(self, "commits", self.commits + 1)
        hook = self.fake.hooks.get(self.kind)
        result = hook(self) if hook else Feature(self.kind)
        object.__setattr__(self, "result", result)
        return result

    def CommitFeature(self):
        return self._commit()

    def Commit(self):
        return self._commit()

    def Destroy(self):
        if self.destroyed:
            raise NXException(3520016, "Builder already destroyed")
        object.__setattr__(self, "destroyed", True)


class Factories(Collection):
    """An NX collection whose ``Create<Kind>Builder`` methods make FakeBuilders."""

    def __init__(self, fake, *items):
        super().__init__(items)
        self.fake = fake

    def __getattr__(self, name):
        if name.startswith("Create") and name.endswith("Builder"):
            kind = name[len("Create") :]
            return lambda *args: self.fake.builder(kind, *args)
        raise AttributeError(name)


# ---- drawing ------------------------------------------------------------------------
class DrawingSheet(Object):
    def __init__(self, fake, name, units="mm", stuck=False):
        super().__init__(name)
        self.fake = fake
        self.Units = units
        self.stuck = stuck  # NX 2406: Open() leaves CurrentDrawingSheet None
        self.views = []

    def Open(self):
        self.fake.opened.append(self.Name)
        self.fake.drawing_work_view = True
        if not self.stuck:
            self.fake.part.DrawingSheets.CurrentDrawingSheet = self

    def GetDraftingViews(self):
        return list(self.views)


class Sheets(Collection):
    CurrentDrawingSheet = None


class ModelingViews:
    """``WorkView`` raises like NX while a drawing sheet is the work view."""

    def __init__(self, fake, views):
        self._fake = fake
        self._views = views

    def __getattr__(self, name):
        if name == "WorkView" and self._fake.drawing_work_view:
            raise RuntimeError("The view is not a model view")
        return getattr(self._views, name)


# ---- the fake session ---------------------------------------------------------------
class FakeTx:
    """The handler-facing members of ``core.tx.Tx`` over a FakeNX."""

    def __init__(self, fake):
        self._fake = fake
        self.touched = False
        self.warnings = []

    @property
    def nx(self):
        return self._fake.nx

    @property
    def uf(self):
        return self._fake.uf

    @property
    def session(self):
        self.touched = True
        return self._fake.session

    @property
    def part(self):
        self.touched = True
        return self._fake.part

    def resolve(self, ref, *kinds):
        entry = self._fake.refs.get(ref)
        if entry is None:
            fail("NX_STALE_REFERENCE", f"{ref} is not a live reference")
        obj, kind = entry
        if kinds and kind not in kinds:
            fail("NX_WRONG_KIND", f"{ref} is a {kind}, expected {' or '.join(kinds)}")
        return obj

    def ref(self, obj, kind=None):
        return self._fake.row(obj, kind)

    def warn(self, code, message):
        self.warnings.append({"code": code, "message": message})


class FakeNX:
    """A fake NX session with one work part in ``units`` (``"mm"`` or ``"in"``)."""

    def __init__(self, units="mm", root=None):
        self.session = Session()
        self.part = Part(self.session, Path(root or "work") / "work.prt", units)
        self.builders: list[FakeBuilder] = []
        self.hooks = {}
        self.missing = {}
        self.errors = {}
        self.refs = {}
        self._ids = itertools.count(1)
        self.opened = []
        self.face_data = {}
        # Display state for view tools (tests/kit/kernel.py KitServices).
        self.camera = {"rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "scale": 1.0}
        self.highlighted = []
        self.display_snapshots = {}
        self.nx = self._namespace()
        self.uf = self._uf()
        self._wire_part()
        self.tx = FakeTx(self)

    # ---- NXOpen namespace -------------------------------------------------------
    def _namespace(self):
        return NS(
            Point3d=point,
            Vector3d=point,
            Matrix3x3=matrix,
            BasePart=NS(Units=UNIT_ENUM),
            Drawings=NS(DrawingSheet=NS(Unit=UNIT_ENUM)),
            Sketch=Sketch,
            SketchWorkRegionBuilder=NS(ScopeType=NS(EntireSketch="all")),
            SmartObject=NS(UpdateOption=NS(WithinModeling=1)),
            Session=NS(MarkVisibility=NS(Visible=1, Invisible=0)),
            Section=NS(Mode=NS(Create=1)),
            Edge=NS(EdgeType=NS(Linear=1, Circular=2)),
            Expression=NS(UnitsOption=NS(Expression="expression")),
            Features=NS(Feature=NS(Null=None)),
        )

    def _uf(self):
        def ask_face_data(tag):
            kind, origin, direction = self.face_data[tag]
            return (FACE_TYPES[kind], list(origin), list(direction), [0] * 6, 0.0, 0.0, 1)

        def open_drawing(tag):
            sheet = next(s for s in self.part.DrawingSheets if s.Tag == tag)
            self.uf_calls.append(("OpenDrawing", tag))
            self.part.DrawingSheets.CurrentDrawingSheet = sheet
            self.drawing_work_view = True

        def set_display_state(state):
            self.uf_calls.append(("SetDisplayState", state))
            if state == 1:
                self.part.DrawingSheets.CurrentDrawingSheet = None
                self.drawing_work_view = False

        self.uf_calls = []
        return NS(
            Modeling=NS(AskFaceData=ask_face_data),
            Draw=NS(OpenDrawing=open_drawing, SetDisplayState=set_display_state),
        )

    def _wire_part(self):
        p = self.part
        p.Features = Factories(self)
        p.Sketches = Factories(self)
        p.BaseFeatures = Factories(self)
        p.Points = NS(CreatePoint=lambda p3: NS(kind="point", Coordinates=p3))
        p.Directions = NS(
            CreateDirection=lambda origin, vector, option: NS(
                kind="direction", Origin=origin, Vector=vector, option=option
            )
        )
        p.ScRuleFactory = NS(
            CreateRuleEdgeDumb=lambda objs: ("EdgeDumb", list(objs)),
            CreateRuleFaceDumb=lambda objs: ("FaceDumb", list(objs)),
            CreateRuleBodyDumb=lambda objs: ("BodyDumb", list(objs)),
            CreateRuleOptions=lambda: NS(Dispose=lambda: None),
            CreateRuleCurveFeature=lambda feats, _a, _o: ("CurveFeature", list(feats)),
        )
        p.ScCollectors = NS(CreateCollector=Collector)
        p.Sections = NS(CreateSection=lambda: NS(rules=[], AddToSection=self._add_to_section))
        p.Expressions = Collection()
        p.Expressions.CreateNumberExpression = self._create_expression
        p.Expressions.EditExpression = self._edit_expression
        p.UnitCollection = NS(FindObject=lambda name: NS(Name=name, Abbreviation=name))
        p.DrawingSheets = Sheets()
        updated = []
        p.DraftingViews = NS(updated=updated, UpdateViews=lambda views: updated.append(list(views)))
        p.Drafting = NS(ExitDraftingApplication=self._exit_drafting)
        p.ModelingViews = ModelingViews(self, p.ModelingViews)
        self.drawing_work_view = False
        self.exit_sticks = False  # NX 2406: the sheet stays the work view after exit

    @staticmethod
    def _add_to_section(rules, *_args):
        return rules

    # ---- builders ---------------------------------------------------------------
    def builder(self, kind, *args):
        b = FakeBuilder(self, kind, args, self.missing.get(kind, ()), self.errors.get(kind))
        self.builders.append(b)
        return b

    def on_commit(self, kind, hook):
        """``hook(builder)`` returns what Commit/CommitFeature returns for ``kind``."""
        self.hooks[kind] = hook

    def lacks(self, kind, *names):
        """Builders of ``kind`` lack these members (an older NX build)."""
        self.missing[kind] = set(names)

    def refuse(self, kind, code=670015, message="NX refused the builder"):
        """Commit of ``kind`` raises a native error with ``code``."""
        self.errors[kind] = NXException(code, message)

    def last(self, kind):
        return next(b for b in reversed(self.builders) if b.kind == kind)

    @property
    def live_builders(self):
        return sum(not b.destroyed for b in self.builders)

    # ---- references ---------------------------------------------------------------
    def row(self, obj, kind=None):
        for ref, (known, known_kind) in self.refs.items():
            if known is obj:
                return {"id": ref, "kind": known_kind, "name": obj.Name, "part": "p:1"}
        kind = kind or type(obj).__name__.lower()
        ref = f"{kind[:2]}:{next(self._ids)}"
        self.refs[ref] = (obj, kind)
        return {"id": ref, "kind": kind, "name": getattr(obj, "Name", ""), "part": "p:1"}

    def ref_of(self, obj, kind=None):
        return self.row(obj, kind)["id"]

    # ---- geometry factories ---------------------------------------------------------
    def _own(self, obj, kind):
        obj.OwningPart = self.part
        obj.IsOccurrence = False
        obj.ref = self.ref_of(obj, kind)
        return obj

    def block(self, x, y, z, origin=(0.0, 0.0, 0.0)):
        """A box body with six planar faces (outward normals) and twelve edges."""
        body = self._own(
            Body("BLOCK", [*origin, origin[0] + x, origin[1] + y, origin[2] + z]), "body"
        )
        ox, oy, oz = origin
        body.faces, body.edges = [], []
        for normal, at in (
            ((0, 0, 1), (ox, oy, oz + z)),
            ((0, 0, -1), (ox, oy, oz)),
            ((1, 0, 0), (ox + x, oy, oz)),
            ((-1, 0, 0), (ox, oy, oz)),
            ((0, 1, 0), (ox, oy + y, oz)),
            ((0, -1, 0), (ox, oy, oz)),
        ):
            body.faces.append(self.face(normal=normal, origin=at, body=body))
        corners = [
            (ox + i * x, oy + j * y, oz + k * z) for i in (0, 1) for j in (0, 1) for k in (0, 1)
        ]
        for a, b in itertools.combinations(corners, 2):
            if sum(p != q for p, q in zip(a, b, strict=True)) == 1:
                body.edges.append(self.edge(a, b, body=body))
        body.face = lambda normal: next(
            f for f in body.faces if tuple(float(v) for v in f.normal) == tuple(map(float, normal))
        )
        self.part.Bodies.append(body)
        return body

    def face(self, normal=(0, 0, 1), origin=(0, 0, 0), kind="plane", body=None):
        f = self._own(Face(f"FACE({kind})"), "face")
        f.normal, f.origin = list(normal), list(origin)
        f.GetBody = lambda: body
        self.face_data[f.Tag] = (kind, origin, normal)
        return f

    def edge(self, start, end, body=None, linear=True):
        e = self._own(Edge("EDGE"), "edge")
        e.SolidEdgeType = self.nx.Edge.EdgeType.Linear if linear else self.nx.Edge.EdgeType.Circular
        e.GetVertices = lambda: (point(*start), point(*end))
        e.GetBody = lambda: body
        return e

    def datum_plane(self, origin=(0, 0, 0), normal=(0, 0, 1)):
        d = self._own(Object("DATUM_PLANE"), "datum_plane")
        d.Origin, d.Normal = point(*origin), point(*normal)
        self.part.Datums.append(d)
        return d

    def datum_axis(self, origin=(0, 0, 0), direction=(0, 0, 1)):
        d = self._own(Object("DATUM_AXIS"), "datum_axis")
        d.Origin, d.Direction = point(*origin), point(*direction)
        self.part.Datums.append(d)
        return d

    def sketch(self, origin=(0, 0, 0), x_axis=(1, 0, 0), y_axis=(0, 1, 0)):
        z = (
            x_axis[1] * y_axis[2] - x_axis[2] * y_axis[1],
            x_axis[2] * y_axis[0] - x_axis[0] * y_axis[2],
            x_axis[0] * y_axis[1] - x_axis[1] * y_axis[0],
        )
        s = Sketch(self.session, "SKETCH")
        s.Origin, s.Orientation = point(*origin), NS(Element=matrix(x_axis, y_axis, z))
        s.Feature = Feature("SKETCH")
        s.updates = []
        s.LocalUpdate = lambda: s.updates.append("LocalUpdate")
        s.Update = lambda: s.updates.append("Update")
        self._own(s, "sketch")
        self.part.Sketches.append(s)
        return s

    def curve(self, sketch=None):
        c = self._own(Object("CURVE"), "curve")
        if sketch is not None:
            sketch.geometry.append(c)
        return c

    def expression(self, name="p1", formula="10", value=10.0):
        e = self._own(Expression(name, formula), "expression")
        e.Value = value
        self.part.Expressions.append(e)
        return e

    def _create_expression(self, spec, unit):
        name, formula = spec.split("=", 1)
        if formula == "invalid":
            raise NXException(1050017, "Syntax error in expression")
        e = self.expression(name, formula)
        e.Units = unit
        return e

    @staticmethod
    def _edit_expression(exp, formula):
        if formula == "invalid":
            raise NXException(1050017, "Syntax error in expression")
        exp.RightHandSide = formula

    # ---- drawing ----------------------------------------------------------------
    def sheet(self, name="SHT1", units="mm", stuck=False):
        s = self._own(DrawingSheet(self, name, units, stuck), "drawing_sheet")
        self.part.DrawingSheets.append(s)
        return s

    def view(self, sheet, name="FRONT@1"):
        v = self._own(Object(name), "drawing_view")
        sheet.views.append(v)
        return v

    def _exit_drafting(self):
        self.opened.append(None)
        self.part.DrawingSheets.CurrentDrawingSheet = None
        if not self.exit_sticks:
            self.drawing_work_view = False

    # ---- undo -------------------------------------------------------------------
    @property
    def undo_marks(self):
        return list(self.session.marks)


def fake_nx(units="mm", root=None):
    """A fresh fake NX session whose work part is in ``units``."""
    return FakeNX(units, root)

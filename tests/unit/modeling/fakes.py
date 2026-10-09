"""Modeling extensions of the shared fake (``tests/kit``) for the modeling unit tests.

``modeling_nx(units)`` is a ``fake_nx`` whose NXOpen namespace auto-creates
enum members (``tx.nx.GeometricUtilities.Extend.ExtendType.Value`` is a stable
node), whose UF answers bounding boxes, face properties, point containment,
body consistency, suppression masks and densities from the fake objects, and
whose mass properties report each body's ``volume`` and ``centroid``.
``feature(nx, kind, bodies)`` makes a committed feature that owns bodies.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from tests.kit import fake_nx
from tests.kit.fake import Node
from tests.kit.objects import Feature, point


class AutoNS:
    """A namespace whose unknown attributes are stable auto-vivifying nodes."""

    def __init__(self, base, path="NXOpen"):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_made", {})

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if hasattr(self._base, name):
            value = getattr(self._base, name)
            if isinstance(value, NS):
                return self._made.setdefault(name, AutoNS(value, f"{self._path}.{name}"))
            return value
        return self._made.setdefault(name, Node(f"{self._path}.{name}"))

    def __setattr__(self, name, value):
        setattr(self._base, name, value)


def _box_center(body):
    b = body.box
    return [(b[i] + b[i + 3]) / 2 for i in range(3)]


def modeling_nx(units="mm"):
    nx = fake_nx(units=units)
    nx.nx = AutoNS(nx.nx)
    nx.nx.Matrix3x3 = lambda: NS()
    part = nx.part
    objects = {}

    def remember(obj):
        objects[int(obj.Tag)] = obj
        return obj

    nx.remember = remember
    make_block = nx.block

    def block(*a, **k):
        body = remember(make_block(*a, **k))
        body.volume = (
            (body.box[3] - body.box[0]) * (body.box[4] - body.box[1]) * (body.box[5] - body.box[2])
        )
        for f in body.faces:
            remember(f)
            f.contains = lambda p: 1
        for e in body.edges:
            remember(e)
        return body

    nx.block = block

    def mass(_units, _accuracy, bodies):
        bodies = list(bodies)
        volume = sum(b.volume for b in bodies)
        weights = [b.volume for b in bodies] or [1]
        centres = [getattr(b, "centroid", None) or _box_center(b) for b in bodies]
        centroid = [
            sum(w * c[i] for w, c in zip(weights, centres, strict=True)) / (sum(weights) or 1)
            for i in range(3)
        ]
        return NS(Volume=volume, Area=0.0, Centroid=point(*centroid), Dispose=lambda: None)

    part.MeasureManager = NS(NewMassProperties=mass)

    def face_props(tag, uv):
        f = objects[tag]
        return (point(), [0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0], list(f.normal), 0, 0)

    def face_data(tag):
        kind, origin, direction = nx.face_data[tag]
        codes = {"plane": 22, "cylinder": 16, "cone": 17, "sphere": 18}
        radius = getattr(objects.get(tag), "radius", 0.0)
        return (codes[kind], list(origin), list(direction), [0] * 6, radius, 0.0, 1)

    nx.suppress_codes = {}
    nx.densities = {}
    nx.faults = {}
    nx.uf = NS(
        Modeling=NS(
            AskFaceData=face_data,
            AskFaceUvMinmax=lambda tag: (0.0, 1.0, 0.0, 1.0),
            AskFaceProps=face_props,
            AskPointContainment=lambda p, tag: objects[tag].contains(p),
            AskBodyConsistency=lambda tag: (nx.faults.get(tag, 0), [], []),
            AskSuppressFeature=lambda tag: nx.suppress_codes.get(int(tag), 0),
            AskBodyDensity=lambda tag, _u: nx.densities.get(int(tag), 7829.0),
        ),
        ModlGeneral=NS(AskBoundingBox=lambda tag: list(objects[tag].box)),
        Draw=nx.uf.Draw,
    )
    part.Axes = NS(CreateAxis=lambda p, d, _o: NS(kind="axis", point=p, direction=d))
    part.Scalars = NS(CreateScalar=lambda v, *_: NS(kind="scalar", value=v))
    made_points = part.Points.CreatePoint

    def create_point(*args):
        if len(args) == 1:
            return made_points(args[0])
        return NS(kind="point_on_curve", args=args, Coordinates=point())

    part.Points = NS(CreatePoint=create_point)
    part.Planes = NS(CreatePlane=lambda *a: Node("plane"))
    part.Xforms = NS(CreateXform=lambda *a: NS(kind="xform", args=a))
    part.CoordinateSystems = NS(CreateCoordinateSystem=lambda *a: NS(kind="csys", args=a))
    part.Datums.CreateFixedDatumPlane = lambda origin, matrix: nx.datum_plane(
        origin=(origin.X, origin.Y, origin.Z), normal=(matrix.Zx, matrix.Zy, matrix.Zz)
    )
    part.CreateExpressionCollectorSet = lambda *a: NS(kind="angle_set", args=a)
    part.ScRuleFactory.CreateRuleFaceDatum = lambda objs: ("FaceDatum", list(objs))
    part.ScRuleFactory.CreateRuleCurveDumbFromPoints = lambda pts: ("Points", list(pts))
    nx.session.DisplayManager = NS(BlankObjects=lambda objs: [o.Blank() for o in objs])
    nx.session.UpdateManager = NS(DoUpdate=lambda mark: nx.update_errors)
    nx.update_errors = 0
    return nx


def feature(nx, kind="EXTRUDE", bodies=(), name=None):
    """A committed feature owning ``bodies`` (added to the part's features)."""
    f = Feature(name or f"{kind}({len(nx.part.Features) + 1})")
    f.FeatureType = kind
    f.bodies = list(bodies)
    f.OwningPart, f.IsOccurrence = nx.part, False
    f.Timestamp = len(nx.part.Features) + 1
    f.parents = []
    f.GetParents = lambda: f.parents
    f.GetFeatureErrorMessages = lambda: list(getattr(f, "errors", []))
    nx.part.Features.append(f)
    return f


def new_body(nx, box, volume=None):
    """A body with ``box`` [x0, y0, z0, x1, y1, z1] added to the part."""
    from tests.kit.objects import Body

    body = Body("BODY", list(box))
    body.OwningPart, body.IsOccurrence = nx.part, False
    body.volume = (
        volume if volume is not None else (box[3] - box[0]) * (box[4] - box[1]) * (box[5] - box[2])
    )
    nx.remember(body)
    nx.part.Bodies.append(body)
    return body


def call(nx, tool, args):
    """``tests.kit.run`` for tools with a ``tool`` argument (it clashes with run's own)."""
    from nx_mcp.core import catalogue, kernel
    from tests.kit.kernel import Result, services

    spec = catalogue.load()[tool]
    return Result(kernel.run(spec, dict(args), kernel.Context(services(nx))))

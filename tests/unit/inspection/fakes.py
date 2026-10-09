"""Inspection extensions of the shared fake (``tests/kit``).

``inspection_nx(units)`` is a ``fake_nx`` whose UF and session answer the inspection
queries from axis-aligned boxes: face data (with the face sense), bounding boxes,
UV samples on planar faces, point containment, ray exits, minimum distances
(box gaps), mass properties (``AskMassProps3d`` in kg and metres), persistent
handles, arcs and curve evaluation. ``box(nx, low, high)`` makes a block body;
``overlap`` interference results are temporary bodies added to the part, so the
kernel's scratch undo is exercised.
"""

from __future__ import annotations

import math
from types import SimpleNamespace as NS

from tests.kit import fake_nx
from tests.kit.objects import Body, Component, Object, point

M_PER = {"mm": 1e-3, "in": 0.0254}
CODES = {"plane": 22, "cylinder": 16, "cone": 17, "sphere": 18}


def _gap(a, b):
    return math.sqrt(sum(max(a[i] - b[i + 3], b[i] - a[i + 3], 0.0) ** 2 for i in range(3)))


def _clamp(p, box):
    return [min(max(p[i], box[i]), box[i + 3]) for i in range(3)]


def inspection_nx(units="mm"):
    nx = fake_nx(units=units)
    part = nx.part
    objects: dict[int, object] = {}
    nx.objects = objects
    nx.density = 7.85e-6 if units == "mm" else 7.85e-6 * 25.4**3  # kg per part unit^3
    nx.inconsistent = {}
    nx.leave_interference = False
    nx.refuse_undo = False

    def remember(obj):
        objects[int(obj.Tag)] = obj
        return obj

    make_block = nx.block

    def block(x, y, z, origin=(0.0, 0.0, 0.0)):
        body = remember(make_block(x, y, z, origin=origin))
        body.volume = x * y * z
        body.JournalIdentifier = f"BLOCK({body.Tag})"
        for f in body.faces:
            remember(f)
            f.sense = 1
            f.JournalIdentifier = f"FACE({f.Tag})"
            f.GetEdges = lambda f=f: []
        for e in body.edges:
            remember(e)
            e.GetFaces = lambda: []
        return body

    nx.block = block

    def face_data(tag):
        f = objects[tag]
        kind, origin, direction = nx.face_data[tag]
        radius = getattr(f, "radius", 0.0)
        return (CODES[kind], list(origin), list(direction), [0] * 6, radius, 0.0, f.sense)

    def body_of(obj):
        return obj if hasattr(obj, "IsSolidBody") else obj.GetBody()

    def face_props(tag, uv):
        f = objects[tag]
        box = body_of(f).box
        n = [float(v) for v in f.normal]
        axis = next(i for i in range(3) if n[i])
        others = [i for i in range(3) if i != axis]
        p = [0.0, 0.0, 0.0]
        p[axis] = box[axis + 3] if n[axis] > 0 else box[axis]
        for k, i in enumerate(others):
            p[i] = box[i] + uv[k] * (box[i + 3] - box[i])
        radii = getattr(f, "radii", [3.937e8, 3.937e8])
        return (list(p), [0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0], n, list(radii))

    def contains(p, tag):
        obj = objects[tag]
        if hasattr(obj, "IsSolidBody"):
            b = obj.box
            inside = all(b[i] < p[i] < b[i + 3] for i in range(3))
            return 1 if inside else 2
        return getattr(obj, "containment", 1)

    def trace(_n, tags, origin, direction, _matrix, _flag):
        body = objects[tags[0]]
        b = body.box
        axis = max(range(3), key=lambda i: abs(direction[i]))
        sign = 1 if direction[axis] > 0 else -1
        target = list(origin)
        target[axis] = b[axis + 3] if sign > 0 else b[axis]
        normal = [0, 0, 0]
        normal[axis] = sign
        face = next(f for f in body.faces if [float(v) for v in f.normal] == normal)
        return 1, [NS(HitPoint=list(target), HitFace=face.Tag)]

    def min_dist(tag_a, tag_b, _ga, _pa, _gb, pb):
        foot = _clamp(list(pb), bounding_box(tag_a))
        return math.dist(foot, pb), foot, list(pb)

    def bounding_box(tag):
        obj = objects[tag]
        box = getattr(obj, "box", None)
        if box is not None:
            return list(box)
        box = list(nx.box_of(obj))
        normal = getattr(obj, "normal", None)
        if normal is not None and getattr(obj, "radius", None) is None:  # planar face
            axis = next(i for i in range(3) if normal[i])
            value = box[axis + 3] if normal[axis] > 0 else box[axis]
            box[axis] = box[axis + 3] = value
        elif getattr(obj, "GetVertices", None) is not None:  # straight edge
            a, b = obj.GetVertices()
            box = [min(a.X, b.X), min(a.Y, b.Y), min(a.Z, b.Z)]
            box += [max(a.X, b.X), max(a.Y, b.Y), max(a.Z, b.Z)]
        return box

    def bounding_exact(tag, _csys):
        b = bounding_box(tag)
        return b[:3], [1, 0, 0, 0, 1, 0, 0, 0, 1], [b[3] - b[0], b[4] - b[1], b[5] - b[2]]

    def mass_props(tags, _n, _type, units_flag, _density, _acc, _accuracy):
        assert units_flag == 4
        m = M_PER[units]
        bodies = [objects[int(t)] for t in tags]
        volume = sum(b.volume for b in bodies)
        mass = sum(b.volume * nx.density for b in bodies)
        centre = [
            sum(b.volume * (b.box[i] + b.box[i + 3]) / 2 for b in bodies) / volume for i in range(3)
        ]
        values = [0.0] * 47
        values[1] = volume * m**3
        values[2] = mass
        values[3:6] = [c * m for c in centre]
        sizes = [bodies[0].box[i + 3] - bodies[0].box[i] for i in range(3)]
        moments = [
            mass * (sizes[1] ** 2 + sizes[2] ** 2) / 12 * m**2,
            mass * (sizes[0] ** 2 + sizes[2] ** 2) / 12 * m**2,
            mass * (sizes[0] ** 2 + sizes[1] ** 2) / 12 * m**2,
        ]
        values[12:15] = moments
        values[31:34] = sorted(moments)
        return values, [0.0] * 11

    def new_mass(_units, _accuracy, bodies):
        bodies = list(bodies)
        k = 1.0 if units == "mm" else 25.4
        volume = sum(b.volume for b in bodies) * k**3
        return NS(Volume=volume, Area=None, Centroid=None, Dispose=lambda: None)

    handles = {}

    def handle_of(tag):
        handles[f"RM{tag} R{tag}"] = tag
        return f"RM{tag} R{tag}"

    def tag_of(handle):
        if handle not in handles:
            raise RuntimeError("bad handle")
        return handles[handle]

    def evaluate_curve(tag, t, _derivs):
        c = objects[tag]
        r = c.radius
        a = t * 2 * math.pi
        if r is None:  # a line along x of length 10
            return [10 * t, 0, 0, 10, 0, 0, 0, 0, 0]
        return [
            r * math.cos(a),
            r * math.sin(a),
            0,
            -r * 2 * math.pi * math.sin(a),
            r * 2 * math.pi * math.cos(a),
            0,
            -r * (2 * math.pi) ** 2 * math.cos(a),
            -r * (2 * math.pi) ** 2 * math.sin(a),
            0,
        ]

    nx.uf = NS(
        Modeling=NS(
            AskFaceData=face_data,
            AskFaceUvMinmax=lambda tag: getattr(objects[tag], "uv_box", (0.0, 1.0, 0.0, 1.0)),
            AskFaceProps=face_props,
            AskPointContainment=contains,
            AskBodyConsistency=lambda tag: nx.inconsistent.get(int(tag), (0, [], [])),
            AskMinimumDist=min_dist,
            TraceARay=trace,
            AskMassProps3d=mass_props,
            AskFaceParm=lambda tag, p: ([0.5, 0.5], list(p)),
        ),
        ModlGeneral=NS(
            AskBoundingBox=bounding_box,
            AskBoundingBoxExact=bounding_exact,
            EvaluateCurve=evaluate_curve,
        ),
        Curve=NS(AskArcData=lambda tag: NS(Radius=objects[tag].radius)),
        Tag=NS(AskHandleFromTag=handle_of, AskTagOfHandle=tag_of),
        Draw=nx.uf.Draw,
    )
    nx.box_of = lambda obj: body_of(obj).box

    def minimum_distance(a, b):
        ba, bb = nx.box_of(a), nx.box_of(b)
        gap = _gap(ba, bb)
        p1 = [(ba[i] + ba[i + 3]) / 2 for i in range(3)]
        p2 = _clamp(p1, bb)
        p1 = _clamp(p2, ba)
        return gap, point(*p1), point(*p2), 0.0

    nx.session.Measurement = NS(GetMinimumDistance=minimum_distance)
    part.MeasureManager = NS(NewMassProperties=new_mass)
    part.WCS = NS(CoordinateSystem=NS(Origin=point(0, 0, 0), Orientation=NS(Element=_identity())))
    part.AnalysisManager = NS(CreateSimpleInterferenceObject=lambda: Interference(nx))
    part.DynamicSections.CreateSectionBuilder = lambda view: SectionBuilder(nx)
    nx.nx.Display = NS(
        DynamicSectionTypes=NS(
            Type=NS(OnePlane=1), CoordinateSystem=NS(Absolute=1), Clip=NS(Section=1)
        )
    )
    nx.nx.GeometricAnalysis = NS(
        SimpleInterference=NS(
            Result=NS(InterferenceExists=1, OnlyEdgesOrFacesInterfere=2, NoInterference=3),
            InterferenceMethod=NS(InterferenceSolid="solid"),
        )
    )
    nx.nx.NXException = lambda code: NS(GetMessage=lambda: f"fault {code}")
    nx.nx.TaggedObjectManager = NS(GetTaggedObject=lambda tag: objects[int(tag)])
    nx.remember = remember
    return nx


def _identity():
    from tests.kit.objects import matrix

    return matrix()


class Interference:
    """``SimpleInterference``: overlapping boxes make a temporary interference body."""

    def __init__(self, nx):
        self.nx = nx
        self.FirstBody = NS(Value=None)
        self.SecondBody = NS(Value=None)
        self.InterferenceType = None
        self.results = []
        self.destroyed = False
        nx.interference_checks = getattr(nx, "interference_checks", 0) + 1

    def PerformCheck(self):
        a, b = self.nx.box_of(self.FirstBody.Value), self.nx.box_of(self.SecondBody.Value)
        low = [max(a[i], b[i]) for i in range(3)]
        high = [min(a[i + 3], b[i + 3]) for i in range(3)]
        sizes = [h - lo for lo, h in zip(low, high, strict=True)]
        if any(s < 0 for s in sizes):
            return 3
        if any(s == 0 for s in sizes):
            return 2
        body = Body("INTERFERENCE", [*low, *high])
        body.volume = sizes[0] * sizes[1] * sizes[2]
        body.OwningPart, body.IsOccurrence = self.nx.part, False
        self.nx.remember(body)
        self.nx.part.Bodies.append(body)
        self.results.append(body)
        return 1

    def GetInterferenceResults(self):
        return list(self.results)

    def Reset(self):
        pass

    def Destroy(self):
        self.destroyed = True


class SectionBuilder:
    """A dynamic section builder; Commit adds a section object to the part."""

    def __init__(self, nx):
        self.nx = nx
        self.destroyed = False

    def SetNormal(self, v):
        self.normal = v

    def SetOrigin(self, p):
        self.origin = p

    def Commit(self):
        section = Object("SECTION")
        self.nx.part.DynamicSections.append(section)
        return section

    def Destroy(self):
        self.destroyed = True


def assembly(nx, *placements):
    """Root + one component per ``(name, box)``; returns the components."""
    root = Component("ROOT")
    root.Prototype = None
    nx.part.ComponentAssembly = NS(RootComponent=root)
    made = []
    for name, box in placements:
        proto = Body(name, list(box))
        component = Component(name, [proto], parent=root)
        component.JournalIdentifier = f"COMPONENT {name} 1"
        for occurrence in component.occurrences.values():
            occurrence.volume = (box[3] - box[0]) * (box[4] - box[1]) * (box[5] - box[2])
            occurrence.OwningPart = nx.part
            nx.remember(occurrence)
        nx.remember(component)
        made.append(component)
    return made


def curve(nx, radius=None):
    """A sketch line (radius None, length 10 along x) or a circle of ``radius``."""
    c = Object("CURVE")
    c.radius = radius
    c.OwningPart, c.IsOccurrence = nx.part, False
    c.ref = nx.ref_of(c, "curve")
    nx.remember(c)
    return c

"""WAVE links and extracted geometry (domains/assembly/wave.py) on the shared fake.

The fake mirrors what NX 2406 showed live: one ``WaveLinkBuilder`` per call (one per
datum or sketch), sources are assembly occurrences, the work part is the target
component's part and the linked copy sits in that part's coordinates.
"""

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from nx_mcp.kit import geom
from tests.kit import fake_nx, run
from tests.kit.fake import Factories
from tests.kit.objects import Body, Face, Feature, Object, Part, matrix, point
from tests.unit.assembly.fakes import nx_matrix

TYPES = NS(BodyLink="body", FaceLink="face", CurveLink="curve", DatumLink="datum",
           SketchLink="sketch")  # fmt: skip
QUARTER_TURN = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]


class Comp(Object):
    """A component occurrence of ``prototype`` at ``(t, R)`` in the assembly."""

    def __init__(self, world, prototype, name, t=(0, 0, 0), r=None):
        super().__init__(name)
        self.Prototype, self.Parent = prototype, None
        self.IsOccurrence, self.IsSuppressed = True, False
        self.OwningPart = world.asm
        self.t, self.r = list(t), r or [list(row) for row in geom.IDENTITY]
        self.occurrences = {}

    def GetPosition(self):
        return point(*self.t), nx_matrix(self.r)

    def FindOccurrence(self, obj):
        return self.occurrences.get(int(obj.Tag))


class World:
    """Assembly ``asm`` (display) with components A (source) and B (work part)."""

    def __init__(self, b_pose=((100, 0, 0), None)):
        nx = self.nx = fake_nx()
        self.asm = nx.part
        nx.nx.Features = NS(
            Feature=NS(Null=None),
            WaveLinkBuilder=NS(Types=TYPES),
            ExtractFaceBuilder=NS(
                ParentPartType=NS(OtherPart="other", WorkPart="work"),
                ExtractType=NS(Body="body", Face="face", RegionOfFaces="region"),
                FaceOptionType=NS(FaceChain="chain"),
            ),
            WaveDatumBuilder=NS(ParentPartType=NS(OtherPart="other")),
        )
        self.a = self._part("a.prt")
        self.b = self._part("b.prt")
        self.A = Comp(self, self.a, "A")
        self.B = Comp(self, self.b, "B", *b_pose)
        self.A.ref = nx.ref_of(self.A, "component")
        self.B.ref = nx.ref_of(self.B, "component")
        parts = nx.session.Parts
        parts.Display, parts.Work, parts.WorkComponent = self.asm, self.b, self.B
        nx.on_commit("WaveLinkBuilder", self._link)
        nx.on_commit("ExtractFaceBuilder", self._extract)
        self.drift = 0.0  # added to every copied volume, area and length

    def _part(self, name):
        nx = self.nx
        p = Part(nx.session, Path(self.asm.FullPath).with_name(name))
        p.BaseFeatures = p.Features = Factories(nx)
        p.ScRuleFactory, p.Sections = self.asm.ScRuleFactory, self.asm.Sections
        p.UnitCollection = self.asm.UnitCollection

        def faces(_a, _b, _acc, found):
            return NS(Area=sum(f.area for f in found), Dispose=lambda: None)

        def mass(_units, _acc, bodies):
            return NS(Volume=sum(b.volume for b in bodies), Dispose=lambda: None)

        p.MeasureManager = NS(NewMassProperties=mass, NewFaceProperties=faces)
        self.asm.MeasureManager = p.MeasureManager
        return p

    # -- objects -----------------------------------------------------------------
    def own(self, obj, part, kind):
        obj.OwningPart, obj.IsOccurrence = part, False
        obj.ref = self.nx.ref_of(obj, kind)
        return obj

    def body(self, part, volume=6000.0, faces=(200.0, 600.0)):
        body = self.own(Body("BODY"), part, "body")
        body.volume = volume
        body.faces = [self.face(part, area, body) for area in faces]
        part.Bodies.append(body)
        return body

    def face(self, part, area, body=None):
        f = self.own(Face("FACE"), part, "face")
        f.area, f.GetBody = area, lambda: body
        return f

    def curve(self, part, length, edge=False):
        c = self.own(Object("EDGE" if edge else "LINE"), part, "edge" if edge else "curve")
        c.GetLength = lambda: length
        if edge:
            c.SolidEdgeType = 1
        return c

    def datum(self, part, kind, origin, direction=None):
        d = self.own(Object(kind.upper()), part, kind)
        d.Origin = point(*origin)
        if kind == "datum_plane":
            d.Normal = point(*direction)
        elif kind == "datum_axis":
            d.Direction = point(*direction)
        else:
            d.Orientation = NS(Element=matrix())
        return d

    def sketch(self, part, lengths):
        s = self.own(Object("SKETCH"), part, "sketch")
        curves = [self.curve(part, n) for n in lengths]
        s.GetAllGeometry = lambda: curves
        return s

    def occurrence(self, obj, comp=None):
        """``obj`` as shown by ``comp`` (geometry moved into assembly coordinates)."""
        comp = comp or self.A
        occ = Object(obj.Name)
        occ.IsOccurrence, occ.OwningComponent, occ.Prototype = True, comp, obj
        occ.OwningPart = self.asm
        for name in ("volume", "area", "GetLength", "SolidEdgeType", "GetAllGeometry"):
            if hasattr(obj, name):
                setattr(occ, name, getattr(obj, name))
        if hasattr(obj, "faces"):
            occ.IsSolidBody, occ.GetFaces = True, lambda: obj.faces
        if hasattr(obj, "Origin"):
            occ.Origin = point(*geom.add(geom.matvec(comp.r, geom.xyz(obj.Origin)), comp.t))
            for name in ("Normal", "Direction"):
                if hasattr(obj, name):
                    setattr(occ, name, point(*geom.matvec(comp.r, geom.xyz(getattr(obj, name)))))
            if hasattr(obj, "Orientation"):
                occ.Orientation = obj.Orientation
        comp.occurrences[int(obj.Tag)] = occ
        return occ

    # -- commits -----------------------------------------------------------------
    def _local(self, p, vector=False):
        """Assembly coordinates -> the work component's part coordinates."""
        r = geom.transpose(self.B.r)
        return geom.matvec(r, p if vector else geom.sub(p, self.B.t))

    def _copy_body(self, sources, faces=False):
        body = Body("LINKED")
        body.OwningPart, body.IsOccurrence = self.nx.session.Parts.Work, False
        if faces:
            body.faces = [self.face(body.OwningPart, f.area + self.drift, body) for f in sources]
            body.volume = 0.0
        else:
            body.volume = sum(s.volume for s in sources) + self.drift
            body.faces = [f for s in sources for f in s.GetFaces()]
        return body

    def _copy_curve(self, c):
        copy = Object("LINKED_CURVE")
        length = c.GetLength() + self.drift
        copy.GetLength = lambda: length
        return copy

    def _copy_datum(self, d):
        out = [Object("POINT")]  # NX adds a point and the CSYS planes and axes
        copy = Object("LINKED_DATUM")
        copy.Origin = point(*self._local(geom.xyz(d.Origin)))
        for name in ("Normal", "Direction"):
            if hasattr(d, name):
                setattr(copy, name, point(*self._local(geom.xyz(getattr(d, name)), True)))
        if hasattr(d, "Orientation"):
            r = geom.matmul(geom.transpose(self.B.r), geom.rows(d.Orientation.Element))
            copy.Orientation = NS(Element=nx_matrix(r))
        return [*out, copy]

    def _feature(self, b, name, bodies=(), entities=()):
        f = Feature(name, list(bodies))
        f.GetFeatureName = lambda: name
        f.GetEntities = lambda: list(entities)
        part = self.nx.session.Parts.Work
        part.Features.append(f)
        part.Bodies.extend(bodies)
        b.GetCommittedObjects = lambda: [f]
        return f

    def _link(self, b):
        kind = b.Type
        if kind in ("body", "face"):
            e = b.ExtractFaceBuilder
            collector = e.ExtractBodyCollector if kind == "body" else e.FaceChain
            sources = collector.ReplaceRules.calls[-1][0][0][0][1]
            return self._feature(b, "LINKED", [self._copy_body(sources, faces=kind == "face")])
        if kind == "curve":
            rules = b.CompositeCurveBuilder.Section.AddToSection.calls[-1][0][0]
            curves = [c for _name, objs in rules for c in objs]
            return self._feature(b, "LINKED_CURVE", entities=map(self._copy_curve, curves))
        if kind == "datum":
            (datum,) = b.WaveDatumBuilder.Datums.Add.calls[-1][0][0]
            return self._feature(b, "LINKED_DATUM", entities=self._copy_datum(datum))
        (sketch,) = b.WaveSketchBuilder.Sketches.Add.calls[-1][0][0]
        curves = [self._copy_curve(c) for c in sketch.Prototype.GetAllGeometry()]
        return self._feature(b, "LINKED_SKETCH", entities=[Object("SKETCH_REF"), *curves])

    def _extract(self, b):
        kind = b.Type
        if kind == "body":
            sources = b.ExtractBodyCollector.ReplaceRules.calls[-1][0][0][0][1]
            return self._feature(b, "EXTRACT_BODY", [self._copy_body(sources)])
        if kind == "face":
            sources = b.FaceChain.ReplaceRules.calls[-1][0][0][0][1]
        else:  # the region: the seed's body faces except the boundary
            (boundary,) = b.BoundaryFaces.Add.calls[-1][0]
            sources = [f for f in b.SeedFace.Value.GetBody().faces if f not in boundary]
        return self._feature(b, "EXTRACT", [self._copy_body(sources, faces=True)])


@pytest.fixture
def w():
    return World()


def link(w, **args):
    return run(w.nx, "nx_create_wave_link", **args)


def test_body_link_from_a_part_object_keeps_the_source_volume(w):
    body = w.body(w.a)
    w.occurrence(body)
    ok = link(w, kind="body", sources=[body.ref], component=w.A.ref)
    assert ok.ok and ok.mutation_outcome == "committed", ok
    assert ok.data["volume"] == 6000 and ok.data["associative"] is True
    assert len(ok.data["features"]) == 1 and len(ok.data["bodies"]) == 1
    b = w.nx.last("WaveLinkBuilder")
    assert b.Type == "body" and b.destroyed and b.args == (None,)
    e = b.ExtractFaceBuilder
    assert (e.ParentPart, e.Associative, e.FixAtCurrentTimestamp) == ("other", True, False)


def test_options_reach_the_builder_and_at_timestamp_needs_a_link(w):
    body = w.body(w.a)
    w.occurrence(body)
    ok = link(w, kind="body", sources=[body.ref], component=w.A.ref, at_timestamp=True)
    assert ok.ok and ok.data["at_timestamp"] is True
    assert w.nx.last("WaveLinkBuilder").ExtractFaceBuilder.FixAtCurrentTimestamp is True
    dumb = link(w, kind="body", sources=[body.ref], component=w.A.ref, associative=False)
    assert dumb.ok and w.nx.last("WaveLinkBuilder").ExtractFaceBuilder.Associative is False
    bad = link(w, kind="body", sources=[body.ref], component=w.A.ref, associative=False,
               at_timestamp=True)  # fmt: skip
    assert bad.error["field"] == "/at_timestamp" and bad.mutation_outcome == "not_started"


def test_face_link_from_an_occurrence_keeps_the_area(w):
    body = w.body(w.a)
    top = w.occurrence(body.faces[0])
    top.ref = w.nx.ref_of(top, "face")
    ok = link(w, kind="face", sources=[top.ref])
    assert ok.ok and ok.data["area"] == 200, ok
    e = w.nx.last("WaveLinkBuilder").ExtractFaceBuilder
    assert e.FaceOption == "chain"


def test_datums_link_one_builder_each_and_lie_on_their_sources():
    w = World(b_pose=((100, 0, 0), QUARTER_TURN))
    plane = w.datum(w.a, "datum_plane", (10, 5, 35), (0, 0, 1))
    axis = w.datum(w.a, "datum_axis", (5, 5, 0), (0, 0, 1))
    csys = w.datum(w.a, "datum_csys", (10, 5, 10))
    for d in (plane, axis, csys):
        w.occurrence(d)
    ok = link(w, kind="datum", sources=[plane.ref, axis.ref, csys.ref], component=w.A.ref)
    assert ok.ok, ok
    assert sum(b.kind == "WaveLinkBuilder" for b in w.nx.builders) == 3
    assert [o["kind"] for o in ok.data["objects"]] == ["datum_plane", "datum_axis", "datum_csys"]
    plane_row, axis_row, csys_row = ok.data["datums"]
    assert plane_row["origin"][2] == pytest.approx(35) and plane_row["normal"] == [0, 0, 1]
    assert axis_row["origin"][:2] == pytest.approx([5, 5])
    assert csys_row["origin"] == pytest.approx([10, 5, 10])
    assert sum(csys_row["axes"], []) == pytest.approx([1, 0, 0, 0, 1, 0, 0, 0, 1])


def test_sketch_and_curve_links_keep_the_length(w):
    sketch = w.sketch(w.a, [20, 10, 20, 10])
    w.occurrence(sketch)
    ok = link(w, kind="sketch", sources=[sketch.ref], component=w.A.ref)
    assert ok.ok and ok.data["length"] == 60 and len(ok.data["objects"]) == 4, ok
    line = w.curve(w.a, 20)
    edge = w.occurrence(w.curve(w.a, 10, edge=True))
    edge.ref = w.nx.ref_of(edge, "edge")
    w.occurrence(line)
    both = link(w, kind="curve", sources=[line.ref, edge.ref], component=w.A.ref)
    assert both.ok and both.data["length"] == 30, both
    assert [s["kind"] for s in both.data["sources"]] == ["curve", "edge"]


def test_refusals_change_nothing(w):
    body = w.body(w.a)
    w.occurrence(body)
    mine = w.body(w.b)
    w.occurrence(mine, w.B)
    parts = w.nx.session.Parts
    cases = [
        ({"kind": "face", "sources": [body.ref], "component": w.A.ref}, "/sources/0"),
        ({"kind": "body", "sources": [body.ref]}, "/component"),
        ({"kind": "body", "sources": [body.ref], "component": w.B.ref}, "/sources/0"),
        ({"kind": "body", "sources": [mine.ref], "component": w.B.ref}, "/sources/0"),
        ({"kind": "body", "sources": [body.ref, body.ref], "component": w.A.ref}, "/sources/1"),
    ]
    for args, field in cases:
        out = link(w, **args)
        assert out.error["field"] == field and out.mutation_outcome == "not_started", (args, out)
    parts.Work = w.asm
    out = link(w, kind="body", sources=[body.ref], component=w.A.ref)
    assert out.error["code"] == "NX_WRONG_PART_CONTEXT"
    assert not any(b.kind == "WaveLinkBuilder" for b in w.nx.builders)
    assert len(w.b.Features) == 0


def test_a_link_that_does_not_match_its_source_rolls_back(w):
    body = w.body(w.a)
    w.occurrence(body)
    w.drift = 1.0
    out = link(w, kind="body", sources=[body.ref], component=w.A.ref)
    assert (
        out.error["code"] == "NX_VERIFICATION_FAILED" and out.error["details"]["check"] == "volume"
    )
    assert out.mutation_outcome == "rolled_back" and len(w.b.Features) == 0
    assert w.nx.last("WaveLinkBuilder").destroyed


def test_extract_body_face_and_region(w):
    body = w.body(w.b, faces=(200, 600, 600, 300, 300, 200))
    top, side, bottom = body.faces[0], body.faces[1], body.faces[5]
    ok = run(w.nx, "nx_create_extract", kind="body", objects=[body.ref])
    assert ok.ok and ok.data["volume"] == 6000 and ok.data["face_count"] == 6, ok
    b = w.nx.last("ExtractFaceBuilder")
    assert (b.ParentPart, b.Type, b.Associative, b.FixAtCurrentTimestamp) == (
        "work", "body", True, False,
    )  # fmt: skip
    faces = run(w.nx, "nx_create_extract", kind="face", objects=[top.ref, side.ref],
                associative=False)  # fmt: skip
    assert faces.ok and faces.data["area"] == 800 and faces.data["associative"] is False
    region = run(w.nx, "nx_create_extract", kind="region", objects=[top.ref],
                 boundary=[bottom.ref])  # fmt: skip
    assert region.ok and region.data["area"] == 2000 and region.data["face_count"] == 5, region
    assert w.nx.last("ExtractFaceBuilder").SeedFace.Value is top


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"kind": "region", "objects": ["$top", "$bottom"], "boundary": ["$bottom"]}, "/objects"),
        ({"kind": "region", "objects": ["$top"]}, "/boundary"),
        ({"kind": "region", "objects": ["$top"], "boundary": ["$top"]}, "/boundary/0"),
        ({"kind": "face", "objects": ["$top"], "boundary": ["$bottom"]}, "/boundary"),
        ({"kind": "body", "objects": ["$top"]}, "/objects/0"),
        ({"kind": "face", "objects": ["$other"]}, "/objects/0"),
    ],
)
def test_extract_refusals(w, args, field):
    body = w.body(w.b)
    other = w.body(w.a)
    names = {"$top": body.faces[0].ref, "$bottom": body.faces[1].ref, "$other": other.faces[0].ref}
    args = {k: [names[x] for x in v] if isinstance(v, list) else v for k, v in args.items()}
    out = run(w.nx, "nx_create_extract", **args)
    assert out.error["field"] == field and out.mutation_outcome == "not_started", out
    assert not any(b.kind == "ExtractFaceBuilder" for b in w.nx.builders)

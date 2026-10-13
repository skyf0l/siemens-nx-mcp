"""Clearance sets on the assembly fake: a ClearanceAnalysisBuilder that stores named sets,
analyses that report hard/soft/touching pairs, overlap solids measured and removed, re-runs,
listing and deletion, and refusals that change nothing."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import NXException, point, run
from tests.kit.objects import Body, Object
from tests.unit.assembly.fakes import assembly_nx, place

SOFT, TOUCH, HARD, CONT12, CONT21, NONE = range(6)
SUCCESS = 1
BETWEEN = NS(Components=0, Bodies=1)
COLLECTIONS = NS(One=0, Two=1)
RANGE = NS(AllObjects=0, AllVisibleObjects=1, SelectedObjects=2, AllButSelectedObjects=3)
METHOD = NS(Lightweight=0, ExactifLoaded=1, Exact=2)


class SelectList:
    def __init__(self):
        self.items = []

    def Add(self, objects):
        self.items.extend(objects)

    def GetArray(self):
        return list(self.items)


class Zone:
    def __init__(self, value):
        self.Type = "Number"
        self.value = float(value)

    def GetValueUsingUnits(self, _option):
        return self.value


class ClearanceSet(Object):
    """A stored set: ``PerformAnalysis`` asks ``nx.clashes`` (``{frozenset(tags): (type,
    p1, p2, depth)}``) about every candidate pair that is not excluded."""

    def __init__(self, nx, settings):
        super().__init__("")
        self.nx = nx
        self.JournalIdentifier = settings["name"]
        self.settings = settings
        self.version = 0
        self.found = []
        self.bodies = {}

    def candidates(self):
        s = self.settings
        if s["range1"] == RANGE.AllObjects:
            first = list(self.nx.assembly.components())
        else:
            first = list(s["one"])
        excluded = {frozenset((int(a.Tag), int(b.Tag))) for a, b in s["exceptions"]}
        if s["count"] == COLLECTIONS.Two:
            pairs = [(a, b) for a in first for b in s["two"]]
        else:
            pairs = [(a, b) for i, a in enumerate(first) for b in first[i + 1 :]]
        return [p for p in pairs if frozenset((int(p[0].Tag), int(p[1].Tag))) not in excluded]

    def PerformAnalysis(self, _option):
        self.version += 1
        self.excluded = len(self.settings["exceptions"])
        before = {frozenset((int(a.Tag), int(b.Tag))) for a, b, _d in self.found}
        self.checked = self.candidates()
        self.found = []
        for a, b in self.checked:
            hit = self.nx.clashes.get(frozenset((int(a.Tag), int(b.Tag))))
            if hit is not None:
                self.found.append((a, b, hit))
        self.new = sum(frozenset((int(a.Tag), int(b.Tag))) not in before for a, b, _d in self.found)

    def GetResults(self):
        if not self.version:
            raise NXException("No results", 3520005)
        kinds = [d[0] for _a, _b, d in self.found]
        return NS(
            Version=self.version,
            NumPairs=len(self.checked) + self.excluded,
            NumCheckedPairs=len(self.checked),
            NumExcludedPairs=self.excluded,
            NumChangedPairs=len(self.checked),
            NumAllInterf=len(kinds),
            NumHard=kinds.count(HARD),
            NumSoft=kinds.count(SOFT),
            NumTouching=kinds.count(TOUCH),
            NumContainment=kinds.count(CONT12) + kinds.count(CONT21),
            NumNewAllInterf=self.new,
        )

    def GetNumberOfInterferences(self):
        return len(self.found)

    def GetNextInterference(self, a, b):
        if a is None:
            index = 0
        else:
            index = next(i for i, f in enumerate(self.found) if f[0] is a and f[1] is b) + 1
        if index >= len(self.found):
            return None, None
        return self.found[index][0], self.found[index][1]

    def GetInterferenceData(self, a, b):
        kind, p1, p2, depth = next(d for x, y, d in self.found if x is a and y is b)
        bodies = [
            x for x in self.bodies.get((int(a.Tag), int(b.Tag)), []) if x in self.nx.asm.Bodies
        ]
        result = SUCCESS if kind == HARD else 0
        return (kind, True, bodies, point(*p1), point(*p2), "", 1, -1, result, depth,
                point(-1, 0, 0), point(0, 0, 0), point(0, 0, 0))  # fmt: skip

    def CreateInterferenceGeometry(self, firsts, seconds):
        for a, b in zip(firsts, seconds, strict=True):
            solid = Body("INTERFERENCE", box=list(self.nx.overlap_box))
            solid.volume = self.nx.overlap_volume
            self.nx.asm.Bodies.append(solid)
            self.bodies.setdefault((int(a.Tag), int(b.Tag)), []).append(solid)

    def Delete(self):
        self.nx.sets.remove(self)


class ClearanceBuilder:
    def __init__(self, nx, cs):
        self.nx = nx
        self.cs = cs
        s = cs.settings if cs is not None else {}
        self.ClearanceSetName = s.get("name", "")
        self.ClearanceBetween = s.get("between", BETWEEN.Components)
        self.TotalCollectionCount = s.get("count", COLLECTIONS.One)
        self.CollectionOneRange = s.get("range1", RANGE.AllObjects)
        self.CollectionTwoRange = RANGE.AllObjects
        self.CollectionOneObjects = SelectList()
        self.CollectionOneObjects.items = list(s.get("one", []))
        self.CollectionTwoObjects = SelectList()
        self.CollectionTwoObjects.items = list(s.get("two", []))
        self.CalculationMethod = s.get("method", METHOD.ExactifLoaded)
        self.IsCalculatePenetrationDepth = False
        self.IsIgnorePairsWithinSamePart = False
        self.zone = s.get("zone")
        self.exceptions = list(s.get("exceptions", []))
        self.destroyed = False
        nx.clearance_builders.append(self)

    def CreateClearanceZoneExpression(self, rhs):
        return Zone(rhs)

    def SetDefaultClearanceZone(self, zone):
        self.zone = zone

    def GetDefaultClearanceZone(self):
        return self.zone

    def AddException(self, exclude, a, b, _text):
        assert exclude is True
        self.exceptions.append((a, b))

    def Commit(self):
        settings = {
            "name": self.ClearanceSetName,
            "between": self.ClearanceBetween,
            "count": self.TotalCollectionCount,
            "range1": self.CollectionOneRange,
            "one": self.CollectionOneObjects.GetArray(),
            "two": self.CollectionTwoObjects.GetArray(),
            "method": self.CalculationMethod,
            "zone": self.zone,
            "exceptions": list(self.exceptions),
        }
        if self.cs is not None:
            self.cs.settings = settings
            return self.cs
        if any(s.JournalIdentifier == self.ClearanceSetName for s in self.nx.sets):
            raise NXException("A clearance analysis data set with the given name already exists")
        cs = ClearanceSet(self.nx, settings)
        self.nx.sets.append(cs)
        return cs

    def Destroy(self):
        self.destroyed = True


def _solid(component, volume=1e6):
    proto = Body("PROTO")
    component.Prototype.Bodies.append(proto)
    occ = Body("OCC")
    occ.IsOccurrence = True
    occ.OwningComponent = component
    occ.volume = volume
    component.occurrences[int(proto.Tag)] = occ
    return occ


@pytest.fixture
def nx():
    nx = assembly_nx()
    nx.sets = []
    nx.clearance_builders = []
    nx.asm.ComponentAssembly.ClearanceSets = nx.sets
    nx.asm.AssemblyManager.CreateClearanceAnalysisBuilder = lambda cs: ClearanceBuilder(nx, cs)
    nx.nx.Assemblies.ClearanceAnalysisBuilder = NS(
        ClearanceBetweenEntity=BETWEEN,
        NumberOfCollections=COLLECTIONS,
        CollectionRange=RANGE,
        CalculationMethodType=METHOD,
    )
    nx.nx.Assemblies.ClearanceSet = NS(
        InterferenceType=NS(
            Soft=SOFT, Touch=TOUCH, Hard=HARD, Cont1In2=CONT12, Cont2In1=CONT21, NoInterference=NONE
        ),  # fmt: skip
        PenetrationDepthResult=NS(NotAttempted=0, Success=SUCCESS),
        ReanalyzeOutOfDateExcludedPairs=NS(CustomerDefault=0),
    )
    session = nx.session
    set_mark, undo = session.SetUndoMark, session.UndoToMark
    saved = {}

    def set_undo_mark(*args):
        mark = set_mark(*args)
        saved[mark] = (list(nx.sets), {id(s): (s.version, list(s.found)) for s in nx.sets})
        return mark

    def undo_to_mark(mark, *args):
        undo(mark, *args)
        sets, states = saved[mark]
        nx.sets[:] = sets
        for s in sets:
            s.version, s.found = states[id(s)]

    session.SetUndoMark, session.UndoToMark = set_undo_mark, undo_to_mark
    nx.a = place(nx, nx.blk, "A")
    nx.b = place(nx, nx.blk, "B", translation=(90, 0, 0))
    nx.c = place(nx, nx.blk, "C", translation=(-105, 0, 0))
    nx.a_body, nx.b_body, nx.c_body = _solid(nx.a), _solid(nx.b), _solid(nx.c)
    tag = int
    nx.clashes = {
        frozenset((tag(nx.a.Tag), tag(nx.b.Tag))): (HARD, (90, 50, 100), (90, 50, 100), 12.2),
        frozenset((tag(nx.a.Tag), tag(nx.c.Tag))): (SOFT, (0, 50, 50), (-5, 50, 50), 0.0),
    }
    nx.overlap_volume = 100000.0
    nx.overlap_box = [90, 0, 0, 100, 100, 100]
    return nx


def test_creates_and_runs_a_set_over_every_component(nx):
    ok = run(nx, "nx_create_clearance_set", part="asm", name="Engine room", clearance=10)
    assert ok.ok and ok.mutation_outcome == "committed", ok
    data = ok.data
    row = data["clearance_set"]
    assert row["name"] == "Engine room" and row["between"] == "components"
    assert row["objects"] is None and row["against"] is None and row["clearance"] == 10
    assert row["method"] == "exact" and row["last_run"]["version"] == 1
    summary = data["summary"]
    assert (summary["hard"], summary["soft"], summary["touching"]) == (1, 1, 0)
    assert summary["checked_pairs"] == 3 and summary["new"] == 2
    hard, soft = data["interferences"]
    assert hard["type"] == "hard" and hard["depth"] == pytest.approx(12.2)
    assert hard["direction"] == [-1, 0, 0]
    assert hard["overlap_volume"] == pytest.approx(100000)
    assert hard["overlap_box"] == {"min": [90, 0, 0], "max": [100, 100, 100]}
    assert [o["name"] for o in hard["objects"]] == ["A", "B"]
    assert soft["type"] == "soft" and soft["distance"] == pytest.approx(5)
    assert "depth" not in soft and "overlap_volume" not in soft
    assert not [b for b in nx.asm.Bodies if b.Name == "INTERFERENCE"]  # scratch removed it
    b = nx.clearance_builders[0]
    assert b.destroyed and b.IsCalculatePenetrationDepth and b.CalculationMethod == METHOD.Exact
    assert b.CollectionOneRange == RANGE.AllObjects and b.zone.value == 10


def test_rerun_after_a_move_reports_the_new_state(nx):
    run(nx, "nx_create_clearance_set", part="asm", name="ER", clearance=10, overlap=False)
    nx.clashes[frozenset((int(nx.a.Tag), int(nx.c.Tag)))] = (TOUCH, (0, 0, 0), (0, 0, 0), 0.0)
    ok = run(nx, "nx_run_clearance_set", part="asm", name="er")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    summary = ok.data["summary"]
    assert summary["version"] == 2 and (summary["soft"], summary["touching"]) == (0, 1)
    assert [i["type"] for i in ok.data["interferences"]] == ["hard", "touching"]
    assert "overlap_volume" in ok.data["interferences"][0]


def test_bodies_two_collections_and_exceptions(nx):
    ok = run(nx, "nx_create_clearance_set", part="asm", name="BODIES", between="bodies",
             objects=[nx.a.ref], against=[nx.b.ref, nx.c.ref],
             exclude=[[nx.a.ref, nx.c.ref]], run=False)  # fmt: skip
    assert ok.ok, ok
    b = nx.clearance_builders[0]
    assert b.ClearanceBetween == BETWEEN.Bodies and b.TotalCollectionCount == COLLECTIONS.Two
    assert b.CollectionOneObjects.items == [nx.a_body]
    assert b.CollectionTwoObjects.items == [nx.b_body, nx.c_body]
    assert b.exceptions == [(nx.a_body, nx.c_body)]
    row = ok.data["clearance_set"]
    assert (row["objects"], row["against"], row["last_run"]) == (1, 2, None)
    assert ok.data["summary"] is None and ok.data["interferences"] == []


def test_refusals_change_nothing(nx):
    run(nx, "nx_create_clearance_set", part="asm", name="ER", run=False)
    body = nx.ref_of(nx.a_body, "body")
    cases = [
        ({"name": "er"}, "NX_ALREADY_EXISTS", "/name"),
        ({"name": "X", "objects": [body, nx.b.ref]}, "NX_WRONG_KIND", "/objects/0"),
        ({"name": "X", "against": [nx.b.ref]}, "NX_INVALID_ARGUMENT", "/objects"),
        ({"name": "X", "objects": [nx.a.ref]}, "NX_INVALID_ARGUMENT", "/objects"),
        ({"name": "X", "objects": [nx.a.ref, nx.a.ref]}, "NX_INVALID_ARGUMENT", "/objects"),
        ({"name": "X", "objects": [nx.a.ref], "against": [nx.a.ref]}, "NX_INVALID_ARGUMENT",
         "/against"),
        ({"name": "X", "clearance": -1}, "NX_INVALID_ARGUMENT", "/clearance"),
    ]  # fmt: skip
    for args, code, field in cases:
        bad = run(nx, "nx_create_clearance_set", part="asm", **args)
        assert bad.error["code"] == code and bad.error["field"] == field, (args, bad)
        assert bad.mutation_outcome == "not_started"
    assert [s.JournalIdentifier for s in nx.sets] == ["ER"]
    for tool in ("nx_run_clearance_set", "nx_delete_clearance_set"):
        missing = run(nx, tool, part="asm", name="nope")
        assert missing.error["code"] == "NX_NOT_FOUND", missing
        assert missing.error["details"]["available"] == ["ER"]


def test_lists_and_deletes_sets(nx):
    run(nx, "nx_create_clearance_set", part="asm", name="ER", clearance=2, run=False)
    run(nx, "nx_create_clearance_set", part="asm", name="AFT", objects=[nx.a.ref, nx.b.ref])
    listed = run(nx, "nx_list_clearance_sets", part="asm")
    assert listed.ok, listed
    items = listed.data["items"]
    assert [i["name"] for i in items] == ["AFT", "ER"]
    assert items[0]["objects"] == 2 and items[0]["last_run"]["hard"] == 1
    assert items[1]["clearance"] == 2 and items[1]["last_run"] is None
    gone = run(nx, "nx_delete_clearance_set", part="asm", name="aft")
    assert gone.ok and gone.data == {"deleted": "AFT", "remaining": ["ER"]}, gone


def test_a_run_nx_does_not_store_rolls_back(nx):
    run(nx, "nx_create_clearance_set", part="asm", name="ER", run=False)
    nx.sets[0].PerformAnalysis = lambda _option: None  # NX stores nothing
    bad = run(nx, "nx_run_clearance_set", part="asm", name="ER")
    assert bad.error["category"] == "verification_failed", bad
    assert bad.mutation_outcome == "rolled_back"

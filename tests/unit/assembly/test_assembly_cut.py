"""nx_create_assembly_cut on the assembly fake: target occurrence bodies cut by tool
occurrence bodies through AssemblyCutBuilder, volumes read back, refusals unchanged."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.kit.objects import Body, Feature
from tests.unit.assembly.fakes import assembly_nx, place


class SelectList:
    def __init__(self):
        self.items = []

    def Add(self, objects):
        self.items.extend(objects)


class CutBuilder:
    """``Features.CreateAssemblyCutBuilder``: Commit removes ``nx.removed`` from every target
    occurrence body and returns a feature whose boolean feature holds the result bodies."""

    def __init__(self, nx, cut):
        self.nx = nx
        self.TargetBody = SelectList()
        self.ToolBody = SelectList()
        self.HideToolBodyFlag = True
        self.destroyed = False
        nx.cut_builders.append(self)

    def Commit(self):
        made = []
        for occ in self.TargetBody.items:
            occ.volume -= self.nx.removed
            result = Body("PROMOTION")
            result.volume = occ.volume + self.nx.drift
            made.append(result)
        boolean = Feature("SUBTRACT", made)
        cut = Feature("ASSEMBLY_CUT")
        cut.GetBooleanBodyFeatures = lambda: [boolean]
        self.nx.asm.Features.append(cut)
        self.nx.asm.Bodies.extend(made)
        return cut

    def GetOperationFailures(self):
        return NS(Length=0, Dispose=lambda: None)

    def Destroy(self):
        self.destroyed = True


def _solid(nx, component, volume):
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
    nx.removed, nx.drift = 80000.0, 0.0
    nx.cut_builders = []
    nx.asm.Features.CreateAssemblyCutBuilder = lambda cut: CutBuilder(nx, cut)
    nx.deck = place(nx, nx.blk, "deck")
    nx.trunk = place(nx, nx.pin, "trunk", translation=(150, 110, -20))
    nx.deck_body = _solid(nx, nx.deck, 1200000.0)
    _solid(nx, nx.trunk, 480000.0)
    nx.asm.MeasureManager = NS(
        NewMassProperties=lambda _u, _a, bodies: NS(
            Volume=sum(b.volume for b in bodies), Dispose=lambda: None
        )
    )
    return nx


def test_cuts_targets_by_tools_and_reads_volumes_back(nx):
    ok = run(nx, "nx_create_assembly_cut", part="asm", targets=[nx.deck.ref],
             tools=[nx.trunk.ref])  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    target = ok.data["targets"][0]
    assert target["volume_before"] == pytest.approx(1200000)
    assert target["volume_after"] == pytest.approx(1120000)
    assert target["removed"] == pytest.approx(80000)
    assert ok.data["tools"][0]["id"] == nx.trunk.ref and len(ok.data["bodies"]) == 1
    b = nx.cut_builders[0]
    assert b.destroyed and b.HideToolBodyFlag is True
    assert b.TargetBody.items == [nx.deck_body]


def test_refusals_change_nothing(nx):
    cases = [
        ({"targets": [nx.deck.ref], "tools": [nx.deck.ref]}, "/tools/0"),
        ({"targets": [nx.deck.ref, nx.deck.ref], "tools": [nx.trunk.ref]}, "/targets/1"),
    ]
    for args, field in cases:
        bad = run(nx, "nx_create_assembly_cut", part="asm", **args)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    empty = place(nx, nx.pin, "empty")
    bare = run(nx, "nx_create_assembly_cut", part="asm", targets=[empty.ref],
               tools=[nx.trunk.ref])  # fmt: skip
    assert bare.error["code"] == "NX_WRONG_GEOMETRY" and bare.error["field"] == "/targets/0"
    nx.deck.IsSuppressed = True
    hidden = run(nx, "nx_create_assembly_cut", part="asm", targets=[nx.deck.ref],
                 tools=[nx.trunk.ref])  # fmt: skip
    assert "suppressed" in hidden.error["message"], hidden
    assert nx.cut_builders == []


def test_a_cut_that_removes_nothing_rolls_back(nx):
    nx.removed = 0.0
    bad = run(nx, "nx_create_assembly_cut", part="asm", targets=[nx.deck.ref],
              tools=[nx.trunk.ref])  # fmt: skip
    assert bad.error["category"] == "verification_failed", bad
    assert bad.mutation_outcome == "rolled_back"
    assert not [f for f in nx.asm.Features if f.Name == "ASSEMBLY_CUT"]
    nx.removed, nx.drift = 80000.0, 5.0  # the boolean result disagrees with the occurrence
    off = run(nx, "nx_create_assembly_cut", part="asm", targets=[nx.deck.ref],
              tools=[nx.trunk.ref])  # fmt: skip
    assert off.error["category"] == "verification_failed", off

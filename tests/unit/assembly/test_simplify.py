"""nx_create_simplified_assembly on the assembly fake: SimplifyBuilder fed with occurrence
solids, size filters, a destination part, the result body read back, refusals unchanged."""

import pytest

from tests.kit import run
from tests.kit.objects import Body
from tests.unit.assembly.fakes import Expr, assembly_nx, place


class SelectList:
    def __init__(self):
        self.items = []

    def Add(self, objects):
        self.items.extend(objects)


class SimplifyBuilder:
    """Commit adds one body of ``nx.simplified_volume`` to the destination (none when 0)."""

    def __init__(self, nx):
        self.nx = nx
        self.DestinationPart = None
        self.ObjectsToSimplify = SelectList()
        self.UseHoleDiameter = self.UseBlendRadius = self.UseBodySize = True
        self.MaximumHoleSize, self.MaximumBlendRadius, self.MinimumBodySize = (
            Expr(),
            Expr(),
            Expr(),
        )
        self.RemoveInternalBodies = self.RemoveInternalVoids = self.UniteBodies = True
        self.destroyed = False
        nx.simplify_builders.append(self)

    def Commit(self):
        if self.nx.simplified_volume:
            body = Body("SIMPLIFIED")
            body.volume = self.nx.simplified_volume
            self.DestinationPart.Bodies.append(body)
        return None

    def Destroy(self):
        self.destroyed = True


def _solid(component, volume=1e6, solid=True):
    proto = Body("PROTO")
    component.Prototype.Bodies.append(proto)
    occ = Body("OCC")
    occ.IsOccurrence = True
    occ.IsSolidBody = solid
    occ.OwningComponent = component
    occ.volume = volume
    component.occurrences[int(proto.Tag)] = occ
    return occ


@pytest.fixture
def nx():
    nx = assembly_nx()
    nx.simplify_builders = []
    nx.simplified_volume = 2.9e6
    nx.asm.AssemblyManager.CreateSimplifyBuilder = lambda: SimplifyBuilder(nx)
    nx.a = place(nx, nx.blk, "A")
    nx.b = place(nx, nx.blk, "B", translation=(90, 0, 0))
    nx.c = place(nx, nx.pin, "C", translation=(-100, 0, 0))
    nx.a_body, nx.b_body = _solid(nx.a), _solid(nx.b)
    nx.c_body = _solid(nx.c)
    return nx


def test_simplifies_every_component_into_the_assembly(nx):
    ok = run(nx, "nx_create_simplified_assembly", part="asm")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    data = ok.data
    assert data["volume"] == pytest.approx(2.9e6)
    assert data["source_volume"] == pytest.approx(3e6) and data["sources"] == 3
    assert data["destination"] == "asm.prt" and len(data["bodies"]) == 1
    b = nx.simplify_builders[0]
    assert b.destroyed and b.DestinationPart is nx.asm
    assert b.ObjectsToSimplify.items == [nx.a_body, nx.b_body, nx.c_body]
    assert (b.UseHoleDiameter, b.UseBlendRadius, b.UseBodySize) == (False, False, False)
    assert b.RemoveInternalBodies and b.RemoveInternalVoids and b.UniteBodies


def test_fills_holes_into_a_destination_part(nx):
    module = nx.new_part("module.prt")
    nx.session.Parts.Work = nx.session.Parts.Display = nx.asm
    ok = run(nx, "nx_create_simplified_assembly", part="asm", objects=[nx.a.ref, nx.b.ref],
             destination_part="module", fill_holes_up_to=50, min_body_size="2 mm",
             unite=False)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["destination"] == "module.prt" and ok.data["sources"] == 2
    b = nx.simplify_builders[0]
    assert b.DestinationPart is module and len(module.Bodies) == 1
    assert b.UseHoleDiameter and b.MaximumHoleSize.Value == 50
    assert b.UseBodySize and b.MinimumBodySize.Value == 2 and not b.UseBlendRadius
    assert b.UniteBodies is False


def test_refusals_and_an_empty_result(nx):
    sheet = place(nx, nx.pin, "SHEET")
    sheet_body = _solid(sheet, solid=False)
    cases = [
        ({"fill_holes_up_to": 0}, "NX_INVALID_ARGUMENT", "/fill_holes_up_to"),
        ({"destination_part": "blk"}, "NX_INVALID_ARGUMENT", "/destination_part"),
        ({"objects": [nx.ref_of(sheet_body, "body")]}, "NX_WRONG_GEOMETRY", "/objects/0"),
        ({"objects": [nx.a.ref, nx.ref_of(nx.a_body, "body")]}, "NX_INVALID_ARGUMENT",
         "/objects"),
    ]  # fmt: skip
    for args, code, field in cases:
        bad = run(nx, "nx_create_simplified_assembly", part="asm", **args)
        assert bad.error["code"] == code and bad.error["field"] == field, (args, bad)
        assert bad.mutation_outcome == "not_started"
    assert nx.simplify_builders == []
    nx.simplified_volume = 0
    empty = run(nx, "nx_create_simplified_assembly", part="asm", objects=[nx.a.ref])
    assert empty.error["category"] == "verification_failed", empty
    assert empty.mutation_outcome in ("rolled_back", "not_started")

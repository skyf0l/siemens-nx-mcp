"""kit.display objects by kind and their occurrences in an assembly."""

from types import SimpleNamespace as NS

from nx_mcp.kit import display

TAGS = iter(range(1000, 2000))


def obj(owner, **extra):
    return NS(Tag=next(TAGS), OwningPart=owner, IsOccurrence=False, **extra)


class Component:
    def __init__(self, name, prototype, parent=None, suppressed=False, refuse=()):
        self.Tag, self.Name, self.JournalIdentifier = next(TAGS), name, name
        self.Prototype, self.Parent, self.IsSuppressed = prototype, parent, suppressed
        self.children, self.refuse, self.made = [], set(refuse), {}
        if parent is not None:
            parent.children.append(self)

    def GetChildren(self):
        return self.children

    def FindOccurrence(self, item):
        if item.Tag in self.refuse:
            raise RuntimeError("no occurrence")
        if item.OwningPart is not self.Prototype:
            return None
        return self.made.setdefault(item.Tag, NS(Tag=next(TAGS), IsOccurrence=True))


def part(name, loaded=True):
    p = NS(Tag=next(TAGS), FullPath=f"{name}.prt", IsFullyLoaded=loaded)
    p.Bodies, p.Sketches, p.Curves, p.Datums, p.CoordinateSystems = [], [], [], [], []
    p.Points = NS(CreatePoint=None)  # not iterable: no point is listed
    return p


def tx():
    calls = NS(checkpoints=0, budget=None)

    def checkpoint(*_a):
        calls.checkpoints += 1

    def scale_budget(items, per_item=0.2):
        calls.budget = items

    return NS(checkpoint=checkpoint, scale_budget=scale_budget, calls=calls)


def assembly(proto, *names, **options):
    asm = part("asm")
    root = Component("root", None)
    asm.ComponentAssembly = NS(RootComponent=root)
    comps = [Component(n, proto, root, **options) for n in names]
    for c in comps:
        c.Parent = None
    return asm, comps


def test_of_kinds_lists_each_kind_once_and_tolerates_a_missing_collection():
    p = part("comp")
    curve = obj(p)
    p.Curves += [curve, curve]
    p.Datums.append(obj(p))
    p.CoordinateSystems.append(obj(p))
    found = display.of_kinds(p, ["curve", "point", "datum"])
    assert found["curve"] == [curve] and found["point"] == [] and len(found["datum"]) == 2


def test_component_kinds_maps_prototype_objects_to_occurrences_per_component():
    proto = part("comp")
    proto.Curves += [obj(proto), obj(proto)]
    proto.Sketches.append(obj(proto))
    asm, comps = assembly(proto, "C1", "C2")
    t = tx()
    found, partial = display.component_kinds(t, asm, ["curve", "sketch"])
    assert len(found["curve"]) == 4 and len(found["sketch"]) == 2 and partial == []
    assert all(o.IsOccurrence for o in found["curve"])
    assert t.calls.budget == 6 and t.calls.checkpoints == 2


def test_component_kinds_skips_suppressed_and_reports_partial_components():
    proto = part("comp", loaded=False)
    proto.Bodies.append(obj(proto))
    proto.Curves.append(obj(proto))  # NX lists none in a partly loaded part
    asm, comps = assembly(proto, "C1")
    Component("C2", proto, asm.ComponentAssembly.RootComponent, suppressed=True)
    found, partial = display.component_kinds(tx(), asm, ["body", "curve"])
    assert len(found["body"]) == 1 and found["curve"] == [] and partial == ["C1"]


def test_occurrence_is_none_when_nx_has_none():
    proto = part("comp")
    item = obj(proto)
    comp = Component("C1", proto, refuse=[item.Tag])
    assert display.occurrence(comp, item) is None


def test_in_assembly_replaces_prototype_objects_of_component_parts_only():
    proto = part("comp")
    asm, comps = assembly(proto, "C1", "C2")
    other = part("other")
    arc, own, stray = obj(proto), obj(asm), obj(other)
    shown = NS(Tag=next(TAGS), IsOccurrence=True, OwningPart=asm)
    out = display.in_assembly(asm, [arc, own, stray, shown])
    assert out[0] is comps[0].made[arc.Tag] and out[1] is comps[1].made[arc.Tag]
    assert out[2:] == [own, stray, shown]

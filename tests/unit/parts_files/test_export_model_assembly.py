"""nx_export_model on an assembly: occurrence bodies of its components, in one file."""

from types import SimpleNamespace as NS

from nx_mcp.domains.parts_files import exchange
from tests.kit import neutral_files
from tests.kit import parts as fake_parts
from tests.kit.objects import Body, Component
from tests.unit.parts_files.support import call

A_BOX = [0, 0, 0, 40, 20, 10]
B_BOX = [100, 0, 0, 140, 20, 10]


def known(nx, *bodies):
    """Let the fake UF bound occurrence bodies (it knows only the loaded parts' bodies)."""
    table = getattr(nx, "occurrence_boxes", None)
    if table is None:
        table = nx.occurrence_boxes = {}
        general = nx.uf.ModlGeneral
        nx.uf.ModlGeneral = NS(
            AskBoundingBox=lambda tag: (
                list(table[tag]) if tag in table else general.AskBoundingBox(tag)
            ),
            AskBoundingBoxExact=lambda tag, csys: (
                fake_parts._exact(table[tag])
                if tag in table
                else general.AskBoundingBoxExact(tag, csys)
            ),
        )
    table.update({b.Tag: b.box for b in bodies})


def assembly(nx, rel="asm.prt", boxes=(A_BOX, B_BOX)):
    """A saved assembly with one component per box (one body each)."""
    part = nx.new_part(rel)
    part.Save()
    root = Component("ROOT")
    for i, box in enumerate(boxes):
        component = Component(f"C{i}", [Body(f"proto{i}", list(box))], parent=root)
        for occurrence in component.occurrences.values():
            occurrence.OwningPart = part  # NX: an occurrence belongs to its assembly
    part.ComponentAssembly.RootComponent = root
    known(nx, *occurrences(root))
    return part, root


def occurrences(root):
    return [o for c in root.GetChildren() for o in c.occurrences.values()]


def export(nx, **args):
    return call(nx, exchange.nx_export_model, **args)


def test_stl_of_an_assembly_writes_every_component_body_in_one_file(nx, root):
    _, top = assembly(nx)
    ok = export(nx, part="asm", path="asm.stl", format="stl")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    stl = nx.exporters[-1]
    assert stl.selected == occurrences(top) and stl.destroyed
    data = ok.data
    assert data["bodies"] == 2 and data["triangles"] == 24
    assert data["bounds"] == {"min": [0, 0, 0], "max": [140, 20, 10]}
    assert (root / "asm.stl").is_file()


def test_occurrence_bodies_pass_through_when_the_assembly_is_the_work_part(nx, root):
    _, top = assembly(nx)
    second = occurrences(top)[1]
    ok = export(nx, part="asm", path="b.stl", format="stl", bodies=[nx.ref_of(second, "body")])
    assert ok.ok, ok
    assert nx.exporters[-1].selected == [second] and ok.data["bodies"] == 1
    assert ok.data["bounds"] == {"min": [100, 0, 0], "max": [140, 20, 10]}


def test_step_counts_the_solids_of_placed_parts_once(nx, root, ugii, monkeypatch):
    part = nx.new_part("asm.prt")
    part.Save()
    top = Component("ROOT")
    proto = Body("proto", list(A_BOX))
    for name in ("A", "B"):  # one part placed twice
        component = Component(name, [proto], parent=top)
        for occurrence in component.occurrences.values():
            occurrence.Prototype, occurrence.OwningPart = proto, part
    part.ComponentAssembly.RootComponent = top
    known(nx, *occurrences(top))
    real = neutral_files.step
    monkeypatch.setattr(fake_parts.neutral_files, "step", lambda n, *a: real(1, *a))
    refs = [nx.ref_of(o, "body") for o in occurrences(top)]
    ok = export(nx, part="asm", path="two.step", format="step", bodies=refs)
    assert ok.ok and ok.data["solids"] == 1 and ok.data["bodies"] == 2, ok
    assert nx.step_creators[-1].selected == occurrences(top)


def test_an_occurrence_outside_the_work_assembly_is_wrong_owner(nx, root):
    _, top = assembly(nx)
    stray = Component("STRAY", [Body("s", [0, 0, 0, 1, 1, 1])])  # no parent: elsewhere
    body = next(iter(stray.occurrences.values()))
    body.OwningPart = nx.session.Parts.Work
    bad = export(nx, part="asm", path="s.stl", format="stl", bodies=[nx.ref_of(body, "body")])
    assert bad.error["code"] == "NX_WRONG_OWNER" and bad.error["field"] == "/bodies/0", bad
    assert bad.mutation_outcome == "not_started" and not nx.exporters
    top.GetChildren()[0].IsSuppressed = True
    hidden = occurrences(top)[0]
    gone = export(nx, part="asm", path="h.stl", format="stl", bodies=[nx.ref_of(hidden, "body")])
    assert gone.error["code"] == "NX_WRONG_OWNER" and "suppressed" in gone.error["message"]


def test_stl_skips_suppressed_components_and_refuses_unloaded_ones(nx, root):
    _, top = assembly(nx)
    top.GetChildren()[0].IsSuppressed = True
    ok = export(nx, part="asm", path="one.stl", format="stl")
    assert ok.ok and ok.data["bodies"] == 1 and ok.data["bounds"]["min"] == [100, 0, 0], ok
    top.GetChildren()[0].IsSuppressed = False
    top.GetChildren()[1].Prototype = NS(Bodies=[], FullPath="C1.prt", IsFullyLoaded=False)
    partial = export(nx, part="asm", path="p.stl", format="stl")
    assert partial.error["code"] == "NX_NOT_LOADED", partial
    assert partial.error["details"]["components"] == ["C1"]
    assert "load_components=true" in partial.error["hint"]
    top.GetChildren()[1].Prototype = None
    unloaded = export(nx, part="asm", path="u.stl", format="stl")
    assert unloaded.error["code"] == "NX_NOT_LOADED" and unloaded.mutation_outcome == "not_started"


def test_jt_writes_a_saved_assembly_natively(nx, root, monkeypatch):
    _, top = assembly(nx)
    real = fake_parts.JtCreator._file
    boxes = [list(o.box) for o in occurrences(top)]  # NX writes the whole display part
    monkeypatch.setattr(
        fake_parts.JtCreator, "_file", lambda self, out, b, u: real(self, out, b or boxes, u)
    )
    ok = export(nx, part="asm", path="asm.jt", format="jt")
    assert ok.ok, ok
    jt = nx.exporters[-1]
    assert jt.JtfileStructure == nx.nx.JtCreatorFileStructure.Monolithic and not jt.selected
    data = ok.data
    assert data["bodies"] == 2 and data["shapes"] == 2 and data["size"] > 0
    assert data["bounds"] == {"min": [0, 0, 0], "max": [140, 20, 10]}


def test_jt_is_refused_while_a_component_part_has_unsaved_changes(nx, root):
    part, top = assembly(nx)
    component = top.GetChildren()[1]
    edited = nx.new_part("c1.prt")
    edited.IsModified = True
    edited.Bodies.extend(component.Prototype.Bodies)
    component.Prototype = edited
    nx.session.Parts.Work = nx.session.Parts.Display = part
    bad = export(nx, part="asm", path="asm.jt", format="jt")
    assert bad.error["code"] == "NX_UNSAVED_PART" and bad.mutation_outcome == "not_started", bad
    assert "1 part(s) of the assembly" in bad.error["message"]
    assert [p["name"] for p in bad.error["details"]["parts"]] == ["c1"]
    assert not nx.exporters


def test_an_assembly_without_any_body_is_not_found(nx, root):
    _, top = assembly(nx)
    for component in top.GetChildren():
        component.IsSuppressed = True
    bad = export(nx, part="asm", path="e.jt", format="jt")
    assert bad.error["code"] == "NX_NOT_FOUND" and bad.mutation_outcome == "not_started", bad


def test_file_translators_get_every_component_body_of_an_assembly(nx, root, ugii):
    # IGES and Parasolid skip components and STEP loads them from the assembly's folder:
    # without bodies, the loaded occurrence bodies are selected explicitly.
    _, top = assembly(nx)
    ok = export(nx, part="asm", path="asm.step", format="step")
    assert ok.ok and ok.data["solids"] == 2 and ok.data["bodies"] == 2, ok
    assert nx.step_creators[-1].selected == occurrences(top)
    ok = export(nx, part="asm", path="asm.igs", format="iges")
    assert ok.ok and ok.data["bodies"] == 2, ok
    assert nx.exporters[-1].selected == occurrences(top)

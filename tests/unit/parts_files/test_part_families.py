"""nx_create_part_family and nx_get_part_family on a fake PartFamily.TemplateManager."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.parts_files import families
from tests.kit import NXException
from tests.kit.objects import Collection, Expression, Object
from tests.unit.parts_files.support import call

KINDS = NS(Instance=7, Expression=9)


class Attr:
    AttrType = KINDS

    def __init__(self, name, kind, value=""):
        self.Name, self.Type, self.Value = name, kind, value


class Definition:
    def __init__(self, name):
        self.name = name
        self.values = {"Part_Name": name}

    def SetValueOfAttribute(self, attr, text):
        self.values[attr.Name] = text

    def GetValueOfAttribute(self, attr):
        return self.values.get(attr.Name, "")


class Family:
    def __init__(self, part, attrs):
        self.part, self.attrs, self.rows = part, attrs, []
        self.folder = None
        self.tag = Object("FAMILY").Tag

    def GetAttributes(self):
        return list(self.attrs)

    def GetMembers(self):
        return [d.name for d in self.rows]


class Manager:
    """NX 2406 behaviour: work part only, columns committed by CreatePartFamily."""

    def __init__(self, fake, part):
        self.fake, self.part = fake, part
        self.chosen = [Attr("Part_Name", KINDS.Instance)]
        self.Importable = True
        self.SaveDirectory = r"C:\nx-working-directory"
        self.family = None
        self.disposed = False
        part.IsModified = True  # NX marks a part without a family modified here
        fake.managers.append(self)

    def _work(self):
        if self.fake.session.Parts.Work is not self.part:
            raise NXException(720034, "Object must belong to the work part.")

    def GetChosenAttributes(self):
        return list(self.chosen)

    def GetSelectableAttributes(self, kind):
        return [e.Name for e in self.part.Expressions if e.Name not in self.fake.locked]

    def AddToChosenAttributes(self, names, kinds, index):
        self._work()
        assert index == len(self.chosen) and all(k == KINDS.Expression for k in kinds)
        exps = {e.Name: e for e in self.part.Expressions}
        self.chosen += [Attr(n, KINDS.Expression, exps[n].RightHandSide) for n in names]

    def CreatePartFamily(self):
        self._work()
        self.family = Family(self.part, list(self.chosen))
        self.part.family = self.family
        return self.family

    def AddInstanceDefinition(self, name, previous, other):
        assert self.family is not None and other == ""
        definition = Definition(name)
        self.family.rows.append(definition)
        return definition

    def SaveFamilyAndCreateMembers(self, definitions):
        folder = Path(self.SaveDirectory)
        self.family.folder = str(folder)
        codes = []
        for d in definitions:
            if d.name in self.fake.member_errors:
                codes.append(self.fake.member_errors[d.name])
                continue
            path = folder / f"{d.name}.prt"
            path.write_bytes(b"NX member")
            self.fake.members[str(path).casefold()] = (self.part, dict(d.values))
            codes.append(0)
        return codes

    def GetInfoMessages(self):
        return ["member failed"]

    def Dispose(self):
        self.disposed = True


@pytest.fixture
def fam(nx, root):
    nx.managers, nx.members, nx.locked, nx.member_errors = [], {}, set(), {}
    template = nx.new_part("t.prt")
    template.Expressions = Collection()
    for name, value in (("length", 40.0), ("width", 20.0), ("p2", 10.0)):
        e = Expression(name, str(int(value)))
        e.Value = value
        template.Expressions.append(e)
    template.family = None
    template.NewPartFamilyTemplateManager = lambda: Manager(nx, template)
    template.Save()
    opened = nx.session.Parts.OpenBase

    def open_member(path):
        part, status = opened(path)
        owner, values = nx.members.get(str(Path(path)).casefold(), (None, {}))
        part.member_of = owner
        part.Expressions = Collection()
        for name, text in values.items():
            if name != "Part_Name":
                e = Expression(name, text)
                e.Value = float(text) if _number(text) else 99.0
                part.Expressions.append(e)
        return part, status

    nx.session.Parts.OpenBase = open_member

    def families_of(tag):
        part = next(p for p in nx.session.Parts if p.Tag == tag)
        family = getattr(part, "family", None)
        return (1, [family.tag]) if family else (0, [])

    def family(tag):
        return next(p.family for p in nx.session.Parts if getattr(p, "family", None)
                    and p.family.tag == tag)  # fmt: skip

    def row(tag, i):
        f = family(tag)
        return NS(Values=[f.rows[i].values.get(a.Name, "") for a in f.attrs])

    attrs = {}

    def family_data(tag):
        f = family(tag)
        for a in f.attrs:
            attrs[id(a)] = a
        return NS(Name=f.part.Name.upper(), Attributes=[id(a) for a in f.attrs],
                  MemberCount=len(f.rows))  # fmt: skip

    nx.uf.Part = NS(
        IsFamilyTemplate=lambda tag: families_of(tag)[0] == 1,
        IsFamilyInstance=lambda tag: _member(nx, tag) is not None,
        IsFamilyInstCurrent=lambda tag: True,
        AskFamilies=families_of,
        AskFamilySaveDir=lambda tag: family(tag).folder + "\\",
    )
    nx.uf.Fam = NS(
        AskFamilyData=family_data,
        AskAttributeData=lambda key: NS(Name=attrs[key].Name),
        AskMemberRowData=row,
    )
    return template


def _member(nx, tag):
    part = next((p for p in nx.session.Parts if p.Tag == tag), None)
    return getattr(part, "member_of", None)


def _number(text):
    try:
        float(text)
    except ValueError:
        return False
    return True


MEMBERS = [
    {"name": "plate_a", "values": [50, 12.5]},
    {"name": "plate_b", "values": ["60", "length/4"]},
]


def create(nx, **args):
    args.setdefault("part", "t")
    args.setdefault("expressions", ["length", "width"])
    args.setdefault("members", MEMBERS)
    return call(nx, families.nx_create_part_family, **args)


def test_creates_members_and_saves_the_template(nx, root, fam):
    ok = create(nx, directory="fam")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    manager = nx.managers[-1]
    assert manager.Importable is False and Path(manager.SaveDirectory) == root / "fam"
    assert [a.Name for a in fam.family.attrs] == ["Part_Name", "length", "width"]
    assert manager.disposed and not fam.IsModified  # saved: it stores the family table
    data = ok.data
    assert data["template_saved"] and data["directory"] == "fam"
    assert data["expressions"] == ["length", "width"]
    a, b = data["members"]
    assert a == {"name": "plate_a", "path": "fam/plate_a.prt",
                 "values": {"length": 50.0, "width": 12.5}, "up_to_date": True}  # fmt: skip
    assert b["values"]["length"] == 60.0  # a formula is stored as given, not checked
    assert fam.family.rows[1].values["width"] == "length/4"
    assert (root / "fam" / "plate_a.prt").is_file()
    assert not [p for p in nx.session.Parts if p.Name.startswith("plate")]  # read, closed


def test_default_folder_is_the_templates(nx, root, fam):
    ok = create(nx, members=[{"name": "m1", "values": [1, 2]}])
    assert ok.ok and ok.data["members"][0]["path"] == "m1.prt", ok


def test_get_part_family_of_template_and_member(nx, root, fam):
    none = call(nx, families.nx_get_part_family, part="t")
    assert none.ok and none.data["role"] == "none" and none.data["members"] == []
    assert create(nx, directory="fam").ok
    got = call(nx, families.nx_get_part_family, part="t")
    assert got.data["role"] == "template" and got.data["template"] == "T"
    assert got.data["columns"] == ["length", "width"] and got.data["directory"] == "fam"
    assert got.data["members"][0] == {"name": "plate_a", "values": {"length": "50",
                                      "width": "12.5"}, "path": "fam/plate_a.prt",
                                      "exists": True}  # fmt: skip
    (root / "fam" / "plate_b.prt").unlink()
    assert call(nx, families.nx_get_part_family, part="t").data["members"][1]["exists"] is False
    member, status = nx.session.Parts.OpenBase(str(root / "fam" / "plate_a.prt"))
    member.GetFamilyInstance = lambda: NS(Family=NS(TemplateName="T"))
    nx.session.Parts.Work = member
    got = call(nx, families.nx_get_part_family, part="plate_a")
    assert got.data["role"] == "member" and got.data["template"] == "T", got
    assert got.data["up_to_date"] is True


def test_refuses_before_nx(nx, root, fam):
    (root / "fam").mkdir()
    (root / "fam" / "plate_b.prt").write_bytes(b"other")
    cases = [
        ({"directory": "fam"}, "NX_ALREADY_EXISTS", "/members/1/name"),
        ({"expressions": ["length", "nope"]}, "NX_NOT_FOUND", "/expressions/1"),
        ({"expressions": ["length"]}, "NX_INVALID_ARGUMENT", "/members/0/values"),
        ({"members": [{"name": "a b", "values": [1, 2]}]}, "NX_INVALID_ARGUMENT",
         "/members/0/name"),
        ({"members": [{"name": "A", "values": [1, 2]}, {"name": "a", "values": [1, 2]}]},
         "NX_INVALID_ARGUMENT", "/members/1/name"),
        ({"members": [{"name": "t", "values": [1, 2]}]}, "NX_ALREADY_EXISTS",
         "/members/0/name"),
        ({"members": [{"name": "m", "values": [1, "x;y"]}]}, "NX_INVALID_ARGUMENT",
         "/members/0/values/1"),
    ]  # fmt: skip
    for extra, code, field in cases:
        bad = create(nx, **extra)
        assert bad.error["code"] == code and bad.error["field"] == field, (extra, bad)
        assert bad.mutation_outcome == "not_started"
    assert not nx.managers and not fam.IsModified
    outside = create(nx, directory="../elsewhere")
    assert outside.error["code"] == "NX_PATH_OUTSIDE_WORKSPACE"


def test_refuses_a_second_family_and_locked_columns(nx, root, fam):
    nx.locked = {"width"}
    bad = create(nx)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/expressions/1"
    assert fam.family is None and nx.managers[-1].disposed
    nx.locked = set()
    assert create(nx, directory="one").ok
    again = create(nx, directory="two", members=[{"name": "x", "values": [1, 2]}])
    assert again.error["code"] == "NX_ALREADY_EXISTS" and again.mutation_outcome == "not_started"


def test_failed_member_removes_written_files(nx, root, fam):
    nx.member_errors = {"plate_b": 5}
    bad = create(nx, directory="fam")
    assert bad.error["code"] == "NX_ERROR" and bad.mutation_outcome == "partial", bad
    assert bad.error["details"]["codes"] == {"plate_a": 0, "plate_b": 5}
    assert "template_note" in bad.error["details"]
    assert not (root / "fam" / "plate_a.prt").exists()


def test_member_values_are_verified(nx, root, fam, monkeypatch):
    real = Manager.SaveFamilyAndCreateMembers

    def wrong(self, definitions):
        codes = real(self, definitions)
        key = next(iter(self.fake.members))
        owner, values = self.fake.members[key]
        self.fake.members[key] = (owner, {**values, "length": "1"})
        return codes

    monkeypatch.setattr(Manager, "SaveFamilyAndCreateMembers", wrong)
    bad = create(nx, directory="fam")
    assert bad.error["code"] == "NX_VERIFICATION_FAILED", bad
    assert "template_note" in bad.error["details"]

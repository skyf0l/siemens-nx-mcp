"""Deformable parts on the fake NX: nx_create_deformable_definition (DeformDefinitionBuilder,
then the 2406 feature-template conversion) and nx_set_component_deformation (DeformManager +
template instantiation: create, edit, formulas, ranges, read back, roll back)."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.kit.attributes import UserAttributes
from tests.kit.objects import Body, Collection, Feature, NXException, Object
from tests.unit.assembly.fakes import assembly_nx, place

MM = NS(Name="MilliMeter", Abbreviation="mm")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class LiveExpr(Object):
    """An expression whose value follows its RightHandSide (a number or a formula of the
    other expressions of ``scope``)."""

    def __init__(self, name, rhs, scope, locked=False):
        super().__init__(name)
        self.Type, self.Units = "Number", MM
        self.IsNoEdit = self.IsRightHandSideLockedFromEdit = self.IsInterpartExpression = False
        self.scope, self.locked, self._rhs = scope, locked, str(rhs)
        self.attributes = {}

    @property
    def RightHandSide(self):  # noqa: N802 - NX name
        return self._rhs

    @RightHandSide.setter
    def RightHandSide(self, text):  # noqa: N802 - NX name
        if self.locked:
            raise NXException(1050001, "This expression cannot be modified because it is locked.")
        self._rhs = str(text)

    @property
    def Value(self):  # noqa: N802 - NX name
        names = set(NAME.findall(self._rhs))
        values = {e.Name: e.Value for e in self.scope() if e is not self and e.Name in names}
        return float(eval(self._rhs, {}, values))  # noqa: S307 - test

    def GetValueUsingUnits(self, _option):
        return self.Value


class Attributed(UserAttributes, Feature):
    def __init__(self, name, kind):
        super().__init__(name)
        self.FeatureType = kind
        self.attributes = {}
        self.parents = []

    def GetParents(self):
        return list(self.parents)


class ExpData:
    def __init__(self):
        self.Type = self.Exp = self.ExpName = self.LowEnd = self.HighEnd = self.ScaleType = None


class DefinitionBuilder:
    def __init__(self, nx, part):
        self.nx, self.part, self.data, self.destroyed = nx, part, [], False
        nx.definition_builders.append(self)

    def SetName(self, name):
        self.name = name

    def SetMemberFeatures(self, members):
        self.members = list(members)

    def SetExpressionsData(self, data, options):
        self.data.append((data, list(options)))

    def CommitFeature(self):
        feature = Attributed(f"{self.name}(9)", "FLEXIBLE_PART")
        feature.expressions = [d.Exp for d, _ in self.data]
        self.part.Features.append(feature)
        return feature

    def Destroy(self):
        self.destroyed = True


class Templates(Collection):
    """``Part.UserDefinedTemplates`` of the deformable part or the assembly."""

    def __init__(self, nx, part):
        super().__init__()
        self.nx, self.part = nx, part

    def ConvertDeformToFeaturetemplate(self, feature):
        again = Attributed(feature.Name, "FLEXIBLE_PART")  # NX recreates the feature
        again.expressions = feature.expressions
        self.part.Features[self.part.Features.index(feature)] = again
        self.append(NS(kind="Definition", GetObjects=lambda: list(self.part.Features)))

    def CreateInstantiationBuilder(self, instantiation):
        return Instantiate(self.nx, self.part, instantiation)

    def SetBuilderAndDefinitionPart(self, builder, part):
        builder.definition_part = part


class Instantiate:
    """``InstantiationBuilder``: create (definition part + Component) or edit."""

    def __init__(self, nx, assembly, instantiation):
        self.nx, self.asm, self.inst = nx, assembly, instantiation
        self.definition_part = self.Component = None
        self.matched = []
        self.destroyed = False

    def _definition(self):
        return next(f for f in self.definition_part.Features if f.FeatureType == "FLEXIBLE_PART")

    def GetExpressions(self):
        if self.inst is not None:
            return list(self.inst.flexed.expressions)
        if self.Component is None:
            return []
        return list(self._definition().expressions)

    def GetMatchedExpression(self, exp):
        taken = {e.Name for e in self.asm.Expressions}
        name, n = exp.Name, 0
        while name in taken:
            n += 1
            name = f"{exp.Name}_{n}"
        m = LiveExpr(name, exp.RightHandSide, lambda: self.asm.Expressions)
        self.asm.Expressions.append(m)
        self.matched.append(m)
        return m, False

    def Commit(self):
        if self.nx.lie:
            for m in self.GetExpressions() if self.inst else self.matched:
                m._rhs = "1"
        if self.inst is not None:
            return self.inst
        comp = self.Component
        ext = self.matched[0]
        body = Body("CYLINDER", [0, 0, 0, 0, 0, 0])
        body.OwningPart, body.IsOccurrence = self.asm, False
        self.nx.boxes[body.Tag] = lambda: [-6.0 + comp.t[0], -6.0, 0.0, 6.0 + comp.t[0], 6.0,
                                           200.0 + ext.Value]  # fmt: skip
        copy = Feature("CYLINDER", [body])
        flexed = Attributed("DEFORMABLE(5)", "FLEXED_PART")
        flexed.expressions, flexed.parents, flexed.component = list(self.matched), [copy], comp
        self.asm.Features.extend([copy, flexed])
        self.asm.Bodies.append(body)
        for m in self.matched:
            m.locked = True
        inst = NS(flexed=flexed, GetObjects=lambda: [flexed, *self.matched])
        self.asm.UserDefinedTemplates.append(inst)
        return inst

    def Destroy(self):
        self.destroyed = True


class DeformBuilder:
    def __init__(self, nx, part, component):
        self.nx, self.part, self.component, self.calls = nx, part, component, []
        self.destroyed = False
        nx.deform_builders.append(self)

    def PreCreateNewDeform(self):
        self.calls.append("pre_create")

    def PostCreateNewDeform(self):
        self.calls.append("post_create")

    def PreUpdateDeform(self):
        self.calls.append("pre_update")
        for f in self.part.Features:  # the builder unlocks the deformation's expressions
            if f.FeatureType == "FLEXED_PART" and f.component is self.component:
                for e in f.expressions:
                    e.locked = False

    def PostUpdateDeform(self, _flag):
        self.calls.append("post_update")

    def Destroy(self):
        self.destroyed = True


@pytest.fixture
def nx():
    nx = assembly_nx()
    nx.lie = False
    nx.definition_builders, nx.deform_builders, nx.preprocessed = [], [], []
    nx.boxes = {}
    blk, asm = nx.blk, nx.asm
    blk.Expressions = Collection()
    asm.Expressions = Collection()

    def scratch(formula, unit):
        """NX's temporary expression evaluating ``formula`` of the assembly's expressions."""
        names = set(NAME.findall(formula))
        values = {e.Name: e.Value for e in asm.Expressions if e.Name in names}
        if names - set(values):
            raise NXException(1050007, "The specified expression variable does not exist.")
        try:
            value = float(eval(formula, {}, values))  # noqa: S307 - test
        except Exception:
            raise NXException(3270008, "The specified string contains a syntax error") from None
        e = LiveExpr(f"scratch_{len(asm.Expressions)}", repr(value), lambda: asm.Expressions)
        e.Units = unit
        asm.Expressions.append(e)
        return e

    asm.Expressions.CreateSystemExpressionWithUnits = scratch
    asm.Expressions.Delete = asm.Expressions.remove
    asm.UnitCollection = NS(FindObject=lambda name: NS(Name=name, Abbreviation=name))
    for name, rhs in (("extension", "100"), ("rod_d", "12")):
        e = LiveExpr(name, rhs, lambda: blk.Expressions)
        e.OwningPart = blk
        blk.Expressions.append(e)
    tube, rod = Feature("CYLINDER(1)"), Feature("CYLINDER(2)")
    blk.Features.extend([tube, rod])
    blk.Features.CreateDeformDefinitionBuilder = lambda: DefinitionBuilder(nx, blk)
    for part in (blk, asm, nx.pin):
        part.UserDefinedTemplates = Templates(nx, part)
    nx.pin.Features = Collection([Feature("BLOCK(1)")])
    nx.nx.Features.DeformDefinitionBuilder = NS(JaDeformDefinitionExpData=ExpData)

    def preprocess(component, part, edit):
        nx.preprocessed.append((component, part, edit))

    manager = NS(
        PreProcessDefinitionPart=preprocess,
        CreateDeformBuilder=lambda part, c: DeformBuilder(nx, part, c),
    )
    nx.nx.Assemblies.DeformManager = NS(GetDeformManager=lambda _session: manager)

    def deformed(tag):
        f = next(f for f in asm.Features if int(f.Tag) == int(tag))
        return NS(PartOcc=f.component.Tag)

    nx.uf.Assem.AskDeformedDefinitionData = deformed
    nx.uf.Assem.SetWorkPartQuietly = nx.session.set_work_quietly
    nx.uf.ModlGeneral = NS(AskBoundingBox=lambda tag: nx.boxes[int(tag)]())
    return nx


def define(nx, **args):
    args.setdefault("expressions", [{"expression": "extension", "min": 50, "max": 150}])
    return run(nx, "nx_create_deformable_definition", part="blk", **args)


def test_define_marks_the_varying_expressions_and_records_the_ranges(nx):
    ok = define(nx, expressions=[{"expression": "extension", "min": 50, "max": 150},
                                 {"expression": "rod_d"}], name="STRUT")  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok.error
    b = nx.definition_builders[0]
    assert (
        b.destroyed
        and b.name == "STRUT"
        and [f.Name for f in b.members]
        == [
            "CYLINDER(1)",
            "CYLINDER(2)",
        ]
    )
    (ext, _), (rod, _) = b.data
    assert (ext.Type, ext.LowEnd, ext.HighEnd, ext.ScaleType) == (1, "50.0", "150.0", 1)
    assert (rod.Type, rod.LowEnd, rod.ExpName) == (0, "", "rod_d")
    flex = next(f for f in nx.blk.Features if f.FeatureType == "FLEXIBLE_PART")
    assert json.loads(flex.attributes["NXMCP_DEFORM_RANGES"]) == {"extension": [50.0, 150.0]}
    rows = ok.data["expressions"]
    assert rows[0]["expression"]["name"] == "extension" and rows[0]["value"] == 100.0
    assert (rows[0]["min"], rows[0]["max"], rows[1]["min"]) == (50.0, 150.0, None)
    assert ok.data["member_features"] == 2 and len(nx.blk.UserDefinedTemplates) == 1


def test_define_refusals_change_nothing(nx):
    cases = [
        ([{"expression": "extension"}, {"expression": "extension"}], "/expressions/1"),
        ([{"expression": "extension", "min": 50}], "/expressions/0"),
        ([{"expression": "extension", "min": 150, "max": 50}], "/expressions/0/min"),
        ([{"expression": "nope"}], "/expressions/0/expression"),
        ([{"expression": "extension", "min": 120, "max": 150}], "/expressions/0"),
    ]
    for expressions, field in cases:
        bad = define(nx, expressions=expressions)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    assert not nx.definition_builders
    assert define(nx).ok
    again = define(nx)
    assert again.error["code"] == "NX_ALREADY_EXISTS" and again.mutation_outcome == "not_started"
    details = again.error["details"]
    assert details["feature"]["kind"] == "feature"
    assert details["ranges"] == {"extension": [50.0, 150.0]}


def deform(nx, component, *values):
    return run(nx, "nx_set_component_deformation", part="asm", component=component.ref,
               values=[{"expression": n, "value": v} for n, v in values])  # fmt: skip


def test_deform_creates_then_edits_and_keeps_the_part_value(nx):
    assert define(nx).ok
    a = place(nx, nx.blk, "A", (100, 0, 0))
    ok = deform(nx, a, ("extension", 150))
    assert ok.ok and ok.data["created"], ok
    row = ok.data["values"][0]
    assert row["expression"] == "extension" and row["value"] == 150.0
    assert row["assembly_expression"]["name"] == "extension" and row["part_value"] == 100.0
    assert ok.data["bounds"]["high"] == [106.0, 6.0, 350.0]
    assert nx.deform_builders[0].calls == ["pre_create", "post_create"]
    assert nx.preprocessed[0][2] is False and all(b.destroyed for b in nx.deform_builders)
    edit = deform(nx, a, ("extension", 80))
    assert edit.ok and not edit.data["created"], edit
    assert edit.data["feature"]["id"] == ok.data["feature"]["id"]
    assert edit.data["bounds"]["high"][2] == 280.0 and edit.data["values"][0]["part_value"] == 100
    assert nx.deform_builders[1].calls == ["pre_update", "post_update"]
    assert nx.preprocessed[1][2] is True
    assert [f.FeatureType for f in nx.asm.Features].count("FLEXED_PART") == 1


def test_a_formula_follows_assembly_expressions(nx):
    assert define(nx).ok
    hatch = LiveExpr("hatch_ext", "90", lambda: nx.asm.Expressions)
    nx.asm.Expressions.append(hatch)
    b = place(nx, nx.blk, "B")
    ok = deform(nx, b, ("extension", "hatch_ext + 10"))
    assert ok.ok, ok
    assert ok.data["values"][0]["formula"] == "hatch_ext + 10"
    assert ok.data["values"][0]["value"] == 100.0


def test_deform_refusals_change_nothing(nx):
    assert define(nx).ok
    a = place(nx, nx.blk, "A")
    pin = place(nx, nx.pin, "P")
    nested = nx.assembly.add(nx.blk, "N", ([0, 0, 0], [[1, 0, 0], [0, 1, 0], [0, 0, 1]]), parent=a)
    nested.ref = nx.ref_of(nested, "component")
    cases = [
        (pin, [("extension", 120)], "/component", "NX_INVALID_ARGUMENT"),
        (nested, [("extension", 120)], "/component", "NX_WRONG_OWNER"),
        (a, [("rod", 120)], "/values/0/expression", "NX_NOT_FOUND"),
        (a, [("extension", 200)], "/values/0/value", "NX_INVALID_ARGUMENT"),
        (a, [("extension", 120), ("extension", 90)], "/values/1", "NX_INVALID_ARGUMENT"),
    ]
    for component, values, field, code in cases:
        bad = deform(nx, component, *values)
        assert bad.error["field"] == field and bad.error["code"] == code, bad
        assert bad.mutation_outcome == "not_started"
    assert not nx.deform_builders


def test_a_formula_outside_the_range_is_refused_before_nx_deforms(nx):
    assert define(nx).ok
    nx.asm.Expressions.append(LiveExpr("hatch_ext", "400", lambda: nx.asm.Expressions))
    a = place(nx, nx.blk, "A")
    bad = deform(nx, a, ("extension", "hatch_ext + 10"))
    assert bad.error["field"] == "/values/0/value" and bad.mutation_outcome == "not_started", bad
    assert "410" in bad.error["message"] and not nx.deform_builders
    assert not [f for f in nx.asm.Features if f.FeatureType == "FLEXED_PART"]


def test_a_bad_formula_is_refused_before_nx_deforms(nx):
    """A failed deform commit leaves NX's deform manager mid-update, so a formula NX would
    refuse is evaluated first and nothing is deformed; the next deformation still works."""
    assert define(nx).ok
    nx.asm.Expressions.append(LiveExpr("hatch_ext", "90", lambda: nx.asm.Expressions))
    a = place(nx, nx.blk, "A")
    names = {e.Name for e in nx.asm.Expressions}
    for formula, code in (
        ("hatch_ext +", "NX_INVALID_FORMULA"),
        ("nope + 1", "NX_INVALID_ARGUMENT"),
    ):
        bad = deform(nx, a, ("extension", formula))
        assert bad.error["code"] == code and bad.error["field"] == "/values/0/value", bad
        assert bad.mutation_outcome == "not_started" and not nx.deform_builders
    assert {e.Name for e in nx.asm.Expressions} == names  # the scratch expressions are gone
    ok = deform(nx, a, ("extension", "hatch_ext + 10"))
    assert ok.ok and ok.data["values"][0]["value"] == 100.0, ok


def test_a_wrong_deformed_value_rolls_back(nx):
    assert define(nx).ok
    a = place(nx, nx.blk, "A")
    nx.lie = True
    bad = deform(nx, a, ("extension", 120))
    assert bad.error["category"] == "verification_failed", bad
    assert bad.mutation_outcome == "rolled_back" and not list(nx.asm.Features)

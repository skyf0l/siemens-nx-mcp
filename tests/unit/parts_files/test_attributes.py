"""nx_get_attributes / nx_set_attributes on a fake NXObject attribute store."""

from __future__ import annotations

import copy
from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.parts_files import attributes as tools
from tests.kit import NXException
from tests.kit.objects import Component, Object
from tests.unit.parts_files.support import call

TYPES = NS(Invalid=0, Null=1, Boolean=2, Integer=3, Real=4, String=5, Time=6, Reference=7, Any=8)


class Info:
    """``NXObject.AttributeInformation``: a mutable struct."""

    def __init__(self, **values):
        self.Title, self.Type, self.Category = "", TYPES.String, ""
        self.StringValue, self.IntegerValue, self.RealValue = "", 0, 0.0
        self.BooleanValue, self.TimeValue = False, ""
        self.Array = self.Locked = self.Inherited = self.OwnedBySystem = False
        self.TitleAlias = ""
        self.__dict__.update(values)


def _store(obj):
    if "_attrs" not in obj.__dict__:
        obj._attrs = {}
    return obj._attrs


def get_attrs(self):
    own = list(_store(self).values())
    prototype = getattr(self, "Prototype", None)
    inherited = [copy.copy(i) for i in _store(prototype).values()] if prototype else []
    for i in inherited:
        i.Inherited = True
    return [copy.copy(i) for i in own] + inherited or [None]  # NX 2406: [None] when empty


def set_attr(self, info, _option):
    store = _store(self)
    key = info.Title.casefold()
    if key == "material":  # NX's alias of the reserved NX_Material
        info = copy.copy(info)
        info.Title, info.TitleAlias, info.OwnedBySystem = "NX_Material", "Material", True
        key = "nx_material"
    old = store.get(key)
    if old is not None and (old.Locked or old.Type != info.Type):
        raise NXException(512042, "Attempted operation on attribute requires privileges")
    new = copy.copy(info)
    if old is not None:
        new.Title = old.Title  # NX keeps the stored spelling
    if new.Type == TYPES.Time:  # NX normalises the date text
        new.StringValue = new.TimeValue
    elif new.Type == TYPES.String:
        pass
    else:
        new.StringValue = str(
            {TYPES.Integer: new.IntegerValue, TYPES.Real: new.RealValue}.get(
                new.Type, new.BooleanValue
            )
        )
    store[key] = new  # like NX, a category not passed again is cleared


def delete_attr(self, kind, title, _whole, _option):
    if _store(self).pop(title.casefold(), None) is None:
        raise NXException(512008, "The attribute not found.")


@pytest.fixture
def anx(nx, monkeypatch):
    nx.nx.NXObject = NS(AttributeType=TYPES, AttributeInformation=Info)
    nx.nx.Update = NS(Option=NS(Now=0))
    for name, fn in (("GetUserAttributes", get_attrs), ("SetUserAttribute", set_attr),
                     ("DeleteUserAttribute", delete_attr)):  # fmt: skip
        monkeypatch.setattr(Object, name, fn, raising=False)
    session = nx.session
    real_mark, real_undo = session.SetUndoMark, session.UndoToMark
    saved = {}
    tracked = []

    def mark(*args):
        m = real_mark(*args)
        saved[m] = [(o, copy.deepcopy(_store(o))) for o in tracked]
        return m

    def undo(m, *args):
        real_undo(m, *args)
        for obj, attrs in saved[m]:
            obj._attrs = attrs

    monkeypatch.setattr(session, "SetUndoMark", mark)
    monkeypatch.setattr(session, "UndoToMark", undo)
    nx.track = tracked.append
    nx.track(nx.part)
    return nx


def _attrs(env, index=0):
    return {a["name"]: a for a in env.data["items"][index]["attributes"]}


def test_round_trip_every_type_on_the_work_part(anx):
    values = [
        {"name": "DB_PART_NO", "type": "string", "value": "100-200", "category": "TITLE"},
        {"name": "DB_PART_DESC", "type": "string", "value": "Bracket, left"},
        {"name": "DB_MATERIAL", "type": "string", "value": "AL6061-T6"},
        {"name": "REVISION", "type": "string", "value": "B"},
        {"name": "QTY", "type": "integer", "value": 4},
        {"name": "WEIGHT", "type": "number", "value": 1.25},
        {"name": "RELEASED", "type": "boolean", "value": True},
        {"name": "DATE", "type": "date", "value": "2026-10-09T12:30"},
    ]
    ok = call(anx, tools.nx_set_attributes, values=values)
    assert ok.ok and ok.mutation_outcome == "committed", ok
    got = _attrs(ok)
    assert got["DB_PART_NO"]["category"] == "TITLE" and got["QTY"]["value"] == 4
    assert got["DATE"]["value"] == "2026-10-09T12:30:00" and got["RELEASED"]["value"] is True
    assert anx.part._attrs["date"].TimeValue == "2026/10/09 12:30:00"
    read = call(anx, tools.nx_get_attributes)
    assert read.ok and read.mutation_outcome == "not_applicable"
    back = _attrs(read)
    for spec in values:
        assert back[spec["name"]]["type"] == spec["type"]
    assert back["WEIGHT"]["value"] == 1.25 and back["DB_MATERIAL"]["value"] == "AL6061-T6"
    only = call(anx, tools.nx_get_attributes, names=["REVISION"])
    assert [a["name"] for a in only.data["items"][0]["attributes"]] == ["REVISION"]


def test_update_and_delete_on_a_body(anx):
    body = anx.block(10, 10, 10)
    anx.track(body)
    ref = anx.ref_of(body, "body")
    call(anx, tools.nx_set_attributes, objects=[ref],
         values=[{"name": "FINISH", "type": "string", "value": "raw"}])  # fmt: skip
    ok = call(anx, tools.nx_set_attributes, objects=[ref],
              values=[{"name": "FINISH", "type": "string", "value": "anodized"}])  # fmt: skip
    assert _attrs(ok)["FINISH"]["value"] == "anodized"
    gone = call(anx, tools.nx_set_attributes, objects=[ref], delete=["FINISH", "MISSING"])
    assert gone.ok and gone.data["items"][0]["deleted"] == ["FINISH"]
    assert call(anx, tools.nx_get_attributes, objects=[ref]).data["items"][0]["attributes"] == []


def test_type_change_needs_delete_in_the_same_call(anx):
    call(anx, tools.nx_set_attributes, values=[{"name": "QTY", "type": "integer", "value": 1}])
    clash = call(anx, tools.nx_set_attributes,
                 values=[{"name": "QTY", "type": "string", "value": "one"}])  # fmt: skip
    assert clash.error["code"] == "NX_ALREADY_EXISTS" and clash.mutation_outcome == "not_started"
    ok = call(anx, tools.nx_set_attributes, delete=["QTY"],
              values=[{"name": "QTY", "type": "string", "value": "one"}])  # fmt: skip
    assert ok.ok and _attrs(ok)["QTY"]["value"] == "one"


def test_locked_and_system_attributes_are_refused(anx):
    _store(anx.part)["pn"] = Info(Title="PN", StringValue="LOCK-1", Locked=True)
    _store(anx.part)["nx_mass"] = Info(Title="NX_Mass", StringValue="x", OwnedBySystem=True)
    for args in ({"values": [{"name": "PN", "type": "string", "value": "X"}]},
                 {"delete": ["PN"]}, {"delete": ["NX_Mass"]}):  # fmt: skip
        refused = call(anx, tools.nx_set_attributes, **args)
        assert refused.error["code"] == "NX_READ_ONLY", refused
        assert refused.mutation_outcome == "not_started"
    assert anx.part._attrs["pn"].StringValue == "LOCK-1"
    read = call(anx, tools.nx_get_attributes)
    assert _attrs(read)["PN"]["locked"] is True and "NX_Mass" not in _attrs(read)
    assert _attrs(call(anx, tools.nx_get_attributes, include_system=True))["NX_Mass"]["system"]


def test_failure_rolls_back_earlier_changes(anx):
    call(anx, tools.nx_set_attributes, values=[{"name": "A", "type": "string", "value": "1"}])

    def boom(self, info, option):
        if info.Title == "B":
            raise NXException(512001, "boom")
        set_attr(self, info, option)

    Object.SetUserAttribute = boom
    bad = call(anx, tools.nx_set_attributes, delete=["A"],
               values=[{"name": "C", "type": "string", "value": "x"},
                       {"name": "B", "type": "string", "value": "y"}])  # fmt: skip
    assert bad.mutation_outcome == "rolled_back" and bad.error["nx_code"] == 512001
    assert set(anx.part._attrs) == {"a"}


def test_component_attributes_are_separate_from_the_prototype(anx):
    component = Component("pin")
    component.Prototype = Object("pin_part")
    anx.track(component)
    anx.track(component.Prototype)
    comp = anx.ref_of(component, "component")
    proto = anx.ref_of(component.Prototype, "part")
    call(anx, tools.nx_set_attributes, objects=[proto],
         values=[{"name": "DB_PART_NO", "type": "string", "value": "PIN-1"}])  # fmt: skip
    ok = call(anx, tools.nx_set_attributes, objects=[comp],
              values=[{"name": "POSITION", "type": "string", "value": "A1"}])  # fmt: skip
    assert ok.ok
    on_comp = _attrs(call(anx, tools.nx_get_attributes, objects=[comp]))
    assert on_comp["POSITION"]["value"] == "A1" and "inherited" not in on_comp["POSITION"]
    assert on_comp["DB_PART_NO"]["inherited"] is True
    on_part = _attrs(call(anx, tools.nx_get_attributes, objects=[proto]))
    assert "POSITION" not in on_part and on_part["DB_PART_NO"]["value"] == "PIN-1"


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        ({"name": "Q", "type": "integer", "value": 1.5}, "/values/0/value"),
        ({"name": "Q", "type": "integer", "value": True}, "/values/0/value"),
        ({"name": "Q", "type": "number", "value": "1"}, "/values/0/value"),
        ({"name": "Q", "type": "boolean", "value": 1}, "/values/0/value"),
        ({"name": "Q", "type": "date", "value": "09.10.2026"}, "/values/0/value"),
        ({"name": "Q", "type": "date", "value": "2026-02-30"}, "/values/0/value"),
        ({"name": "Q", "type": "date", "value": "1960-01-01"}, "/values/0/value"),
        ({"name": "Q", "type": "string", "value": 3}, "/values/0/value"),
    ],
)
def test_values_are_checked_before_nx(anx, spec, field):
    bad = call(anx, tools.nx_set_attributes, values=[spec])
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == field, bad
    assert bad.mutation_outcome == "not_started" and "q" not in _store(anx.part)


def test_empty_or_duplicate_requests_are_refused(anx):
    assert call(anx, tools.nx_set_attributes).error["code"] == "NX_INVALID_ARGUMENT"
    twice = [{"name": "A", "type": "string", "value": "1"}] * 2
    assert call(anx, tools.nx_set_attributes, values=twice).error["code"] == "NX_INVALID_ARGUMENT"


def test_update_is_case_insensitive_and_keeps_the_category(anx):
    call(anx, tools.nx_set_attributes,
         values=[{"name": "DB_PART_REV", "type": "string", "value": "A", "category": "TB"}])  # fmt: skip
    ok = call(anx, tools.nx_set_attributes,
              values=[{"name": "db_part_rev", "type": "string", "value": "B"}])  # fmt: skip
    row = _attrs(ok)["DB_PART_REV"]
    assert row["value"] == "B" and row["category"] == "TB" and len(anx.part._attrs) == 1
    only = call(anx, tools.nx_get_attributes, names=["Db_Part_Rev"])
    assert [a["name"] for a in only.data["items"][0]["attributes"]] == ["DB_PART_REV"]
    gone = call(anx, tools.nx_set_attributes, delete=["db_part_REV"])
    assert gone.data["items"][0]["deleted"] == ["DB_PART_REV"] and not anx.part._attrs


def test_reserved_alias_is_refused_and_rolled_back(anx):
    bad = call(anx, tools.nx_set_attributes,
               values=[{"name": "MATERIAL", "type": "string", "value": "steel"}])  # fmt: skip
    assert bad.error["code"] == "NX_READ_ONLY" and bad.error["field"] == "/values/0/name", bad
    assert bad.mutation_outcome == "rolled_back" and not anx.part._attrs

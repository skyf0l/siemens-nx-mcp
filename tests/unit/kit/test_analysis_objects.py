"""kit.analysis_objects: the displayed-part guard, remembered analysis objects and their
deletion before a part closes."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import analysis_objects as ao


class Session:
    def __init__(self, display):
        self.Parts = NS(Display=display)
        self.deleted, self.marks, self.errors = [], [], 0
        self.UpdateManager = NS(
            ClearErrorList=lambda: None,
            AddToDeleteList=lambda objs: self.deleted.extend(objs),
            DoUpdate=lambda mark: self.errors,
        )

    def SetUndoMark(self, visibility, name):  # noqa: N802 - NXOpen name
        self.marks.append((visibility, name))
        return len(self.marks)


def obj(tag):
    return NS(Tag=tag)


def make_tx(part, display, dead=()):
    return NS(
        part=part,
        session=Session(display),
        nx=NS(Session=NS(MarkVisibility=NS(Invisible="Invisible"))),
        uf=NS(Obj=NS(AskStatus=lambda tag: 2 if tag in dead else ao.UF_OBJ_ALIVE)),
    )


def test_only_the_displayed_part_takes_analysis_objects():
    part, other = obj(1), obj(2)
    ao.require_displayed(make_tx(part, part))
    assert ao.is_displayed(make_tx(part, obj(1)))
    for display in (other, None):
        with pytest.raises(NXError) as error:
            ao.require_displayed(make_tx(part, display), field="/type")
        assert error.value.code == "NX_WRONG_PART_CONTEXT" and error.value.field == "/type"
        assert "nx_activate_part" in error.value.hint


def test_delete_all_removes_remembered_and_listed_objects_once():
    part = obj(10)
    made, undone, listed = obj(11), obj(12), obj(13)
    part.AnalysisManager = NS(AnalysisObjects=[made, listed])
    ao.remember(part, [made, undone])
    ao.remember(part, [made])  # no duplicate
    tx = make_tx(part, part, dead={12})
    assert [o.Tag for o in ao.of_part(tx, part)] == [11, 13]
    assert ao.delete_all(tx, part) == 2
    assert [o.Tag for o in tx.session.deleted] == [11, 13]
    assert tx.session.marks == [("Invisible", "NX MCP analysis cleanup")]
    part.AnalysisManager = NS(AnalysisObjects=None)  # forgotten after the delete
    assert ao.delete_all(tx, part) == 0 and len(tx.session.marks) == 1


def test_delete_all_reports_update_errors():
    part = obj(20)
    ao.remember(part, [obj(21)])
    tx = make_tx(part, part)
    tx.session.errors = 1
    with pytest.raises(NXError) as error:
        ao.delete_all(tx, part)
    assert error.value.code == "NX_UPDATE_FAILED"


def named_obj(tag, name):
    return NS(Tag=tag, Name=name)


def test_names_are_unique_per_part_and_found_in_any_case():
    part = obj(30)
    part.AnalysisManager = NS(AnalysisObjects=[named_obj(31, "NX_MCP_RADIUS_1")])
    ao.remember(part, [named_obj(32, "NX_MCP_RADIUS_2"), named_obj(33, "ENGINEER_ZEBRA")])
    tx = make_tx(part, part)
    assert ao.names(tx, part) == ["NX_MCP_RADIUS_1", "NX_MCP_RADIUS_2"]
    assert ao.unique_name(tx, part, "radius") == "NX_MCP_RADIUS_3"
    assert ao.unique_name(tx, part, "slope") == "NX_MCP_SLOPE_1"
    assert [o.Tag for o in ao.named(tx, part, "nx_mcp_radius_2")] == [32]
    ao.delete_all(tx, part)


def test_named_finds_unlisted_objects_of_the_work_part_through_uf():
    part = obj(40)
    hidden = named_obj(41, "NX_MCP_REFLECTION_1")
    tx = make_tx(part, part)
    tx.session.Parts.Work = part
    tags = iter([41, 0])
    tx.uf.Obj.CycleByName = lambda name, tag: next(tags) if name == hidden.Name else 0
    tx.nx.TaggedObjectManager = NS(GetTaggedObject=lambda tag: hidden)
    assert ao.named(tx, part, "nx_mcp_reflection_1") == [hidden]
    tx.session.Parts.Work = obj(99)  # UF cycles the work part only
    assert ao.named(tx, part, "NX_MCP_REFLECTION_1") == []


def test_delete_takes_the_objects_and_their_unused_expressions():
    part = obj(50)
    used, free, other = (NS(Tag=t, Name=n) for t, n in ((51, "p7"), (52, "p8"), (53, "p9")))
    used.GetReferencingExpressions = lambda: [other]
    free.GetReferencingExpressions = list

    class Expressions(list):
        def Delete(self, exp):  # noqa: N802 - NXOpen name
            self.remove(exp)

    part.Expressions = Expressions([used, free, other])
    made = named_obj(54, "NX_MCP_CONTINUITY_1")
    ao.remember(part, [made], expressions=["p7", "p8"])
    dead: set = set()
    tx = make_tx(part, part, dead=dead)
    tx.session.UpdateManager.AddToDeleteList = lambda objs: dead.update(o.Tag for o in objs)
    assert ao.delete(tx, part, "nx_mcp_continuity_1") == (1, ["p8"])
    assert [e.Name for e in part.Expressions] == ["p7", "p9"]  # p7 is still in use
    assert ao.delete(tx, part, "NX_MCP_CONTINUITY_1") == (0, [])

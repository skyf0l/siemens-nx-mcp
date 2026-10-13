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

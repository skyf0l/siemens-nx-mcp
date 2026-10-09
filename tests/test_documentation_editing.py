"""Contracts, preflight and transaction regressions; native geometry tested separately."""

import inspect
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, Mock

import pytest

from nx_mcp import documentation_editing_server
from nx_mcp.hardened import READ_ONLY
from nx_mcp.runtime import NXToolError
from tests.test_freeform_manufacturing import ff as _freeform_fixture


@pytest.fixture
def ff(rig, monkeypatch):
    return _freeform_fixture.__wrapped__(rig, monkeypatch)


def test_contracts(rig):
    for name, fn in vars(documentation_editing_server).items():
        if name.startswith("nx_") and inspect.isfunction(fn):
            assert (
                inspect.signature(fn).parameters.keys()
                == inspect.signature(rig.e._handlers[name]).parameters.keys()
            )
    assert documentation_editing_server.READ_ONLY <= READ_ONLY


def test_annotation_refresh_failure_rolls_back_the_model_edit(rig):
    rig.e._handlers["nx_extrude"] = Mock(return_value={})
    rig.e._refresh_annotations = Mock(
        side_effect=NXToolError("NX_STALE_ANNOTATION_SOURCE", "stale source")
    )
    rig.session.UndoToMark = Mock()
    with pytest.raises(NXToolError) as error:
        rig.e.execute("nx_extrude", {})
    assert error.value.details["mutation_outcome"] == "rolled_back"
    rig.session.UndoToMark.assert_called_once()


def test_automatic_bend_table_rebuild_and_opt_out(ff):
    from tests.fakes import Object

    table = Object("bend table")
    builder = MagicMock()
    builder.Style.BendTable.AutomaticUpdate = True
    builder.Commit.return_value = table

    class Tables(list):
        def CreateBendTableBuilder(self, existing):
            assert existing is table
            return builder

    ff.part.Annotations = NS(BendTables=Tables([table]))
    ff.e._documentation_annotations = lambda _: []
    ff.e._table_cells = Mock(side_effect=[[["80"]], [["85"]]])
    assert ff.e._refresh_annotations()["updated_count"] == 1
    builder.Commit.assert_called_once()
    builder.Destroy.assert_called_once()
    builder.reset_mock()
    builder.Style.BendTable.AutomaticUpdate = False
    assert ff.e._refresh_annotations()["updated_count"] == 0
    builder.Commit.assert_not_called()
    builder.Destroy.assert_called_once()

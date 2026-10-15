"""Core seams the session tools use: the Arguments (Json) type, tx.preview outcomes,
tx.scratch's mark and receipt progress totals."""

from types import SimpleNamespace as NS

import pytest
from pydantic import TypeAdapter

from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.receipts import Receipts
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Arguments, Json, check_json
from nx_mcp.core.validate import validate
from nx_mcp.sidecar.schema import _Converter
from tests.kit import fake_nx
from tests.kit.kernel import services

SPEC = NS(cost="light", effect="session", name="nx_test", label="Test")
PARAMS = {"arguments": NS(name="arguments", hint=Arguments, default=None, required=True)}


@pytest.mark.parametrize(
    "value, ok",
    [({}, True), ({"a": [1, "x"]}, True), ([], False), ({"a": float("nan")}, False),
     ({str(i): i for i in range(41)}, False), ({"a": "x" * 17000}, False)],
)  # fmt: skip
def test_arguments_are_bounded_json_in_both_validators(value, ok):
    adapter = TypeAdapter(_Converter()(Arguments))
    try:
        validate(PARAMS, {"arguments": value})
        core = True
    except NXError:
        core = False
    try:
        adapter.validate_python(value)
        pyd = True
    except Exception:
        pyd = False
    assert core == pyd == ok
    with pytest.raises(ValueError):
        check_json("x", Json())


def tx_for(nx):
    return Tx(services(nx), SPEC, operation_id="op_testtest")


def test_preview_undoes_and_reports_previewed():
    nx = fake_nx()
    tx = tx_for(nx)
    with tx.preview():
        nx.block(1, 1, 1)
        assert tx.mode == "preview"
    assert tx.outcome == "previewed" and list(nx.part.Bodies) == [] and nx.undo_marks == []


def test_preview_keeps_the_original_error_when_rollback_fails():
    nx = fake_nx()
    tx = tx_for(nx)
    nx.session.UndoToMark = lambda *_: (_ for _ in ()).throw(RuntimeError("undo broke"))
    with pytest.raises(NXError) as error, tx.preview():
        fail("NX_BUILD_FAILED", "child failed")
    assert error.value.code == "NX_BUILD_FAILED" and tx.outcome == "partial"
    assert "undo broke" in error.value.details["rollback_error"]
    tx2 = tx_for(nx)
    with pytest.raises(NXError) as error, tx2.preview():
        pass
    assert error.value.code == "NX_ROLLBACK_FAILED" and tx2.outcome == "partial"


def test_preview_failure_with_good_rollback_is_rolled_back():
    nx = fake_nx()
    tx = tx_for(nx)
    with pytest.raises(NXError), tx.preview():
        nx.block(1, 1, 1)
        fail("NX_BUILD_FAILED", "child failed")
    assert tx.outcome == "rolled_back" and list(nx.part.Bodies) == []


def test_scratch_yields_its_invisible_mark():
    nx = fake_nx()
    tx = tx_for(nx)
    with tx.scratch() as mark:
        assert mark in nx.session.marks
    assert nx.undo_marks == []


def test_progress_keeps_the_announced_total(tmp_path):
    store = services(fake_nx()).store
    receipts = Receipts(store, "s")
    record, _ = receipts.begin("nx_test", {}, "op_progress1", None)
    receipts.progress(record, 0, 5)
    receipts.progress(record, 2)
    assert store.get("op_progress1")["progress"]["total"] == 5


def test_a_replace_waits_while_windows_holds_the_earlier_file(tmp_path, monkeypatch):
    from nx_mcp.core import tx as tx_module

    real, calls = tx_module.os.replace, []

    def held(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError(13, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(tx_module.os, "replace", held)
    monkeypatch.setattr(tx_module.time, "sleep", lambda _s: None)
    (tmp_path / "new").write_text("b")
    (tmp_path / "old").write_text("a")
    tx_module.replace_file(tmp_path / "new", tmp_path / "old")
    assert (tmp_path / "old").read_text() == "b" and len(calls) == 3
    monkeypatch.setattr(tx_module, "REPLACE_TRIES", 2)
    calls.clear()
    (tmp_path / "new").write_text("c")
    with pytest.raises(PermissionError):
        tx_module.replace_file(tmp_path / "new", tmp_path / "old")

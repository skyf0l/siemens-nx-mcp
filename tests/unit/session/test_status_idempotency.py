"""NX_IDEMPOTENCY_CONFLICT (core/receipts.py): an operation_id reused for another call
names the field, the earlier call's tool and the fix (a new operation_id)."""

from __future__ import annotations

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.core.receipts import OperationStore, Receipts


def test_a_reused_id_with_other_arguments_says_to_pick_a_new_one(tmp_path):
    receipts = Receipts(OperationStore(tmp_path), "s")
    record, _ = receipts.begin("nx_create_block", {"length": 40}, "op_reused_01", "p")
    receipts.finish(record, {"ok": True, "mutation_outcome": "committed", "data": {}})
    with pytest.raises(NXError) as raised:
        receipts.replay("nx_create_block", {"length": 41}, "op_reused_01")
    error = raised.value.error_dict()
    assert error["code"] == "NX_IDEMPOTENCY_CONFLICT" and error["field"] == "/operation_id"
    assert error["retry"] == "after_fix" and "new operation_id" in error["hint"]
    assert error["details"]["tool"] == "nx_create_block"
    assert "nx_create_block" in error["message"]


def test_the_same_call_in_another_part_names_the_field(tmp_path):
    receipts = Receipts(OperationStore(tmp_path), "s")
    record, _ = receipts.begin("nx_set_name", {"name": "A"}, "op_parts_001", "p", part_file="a")
    receipts.finish(record, {"ok": True, "mutation_outcome": "committed", "data": {}})
    with pytest.raises(NXError) as raised:
        receipts.replay("nx_set_name", {"name": "A"}, "op_parts_001", "b")
    assert raised.value.field == "/operation_id" and raised.value.retry == "after_fix"

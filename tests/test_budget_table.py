"""The generated token budget table: caps from test_budget, values from its measurement."""

import importlib.util
from pathlib import Path

from tests.sidecar import test_budget

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/generate_budget_table.py"
SPEC = importlib.util.spec_from_file_location("budget_table", SCRIPT)
assert SPEC and SPEC.loader
table = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(table)


def test_the_table_shows_each_cap_with_its_measured_value():
    measured = test_budget.measured_now()
    rendered = table.render(test_budget, measured)
    assert rendered.startswith(table.BEGIN) and rendered.endswith(table.END)
    rows = [line for line in rendered.splitlines() if line.startswith("| ")]
    assert len(rows) == 10  # the header and nine budgets
    assert f"{test_budget.AGENT_LISTING:,}" in rendered
    assert f"{measured['agent_listing']:,}" in rendered
    assert f"{test_budget.TYPICAL_TASK:,}" in rendered


def test_only_the_marked_section_is_replaced():
    doc = f"before\n{table.BEGIN}\nold\n{table.END}\nafter\n"
    assert table.replace(doc, "NEW") == "before\nNEW\nafter\n"
    assert table.BEGIN in table.DOC.read_text(encoding="utf-8")

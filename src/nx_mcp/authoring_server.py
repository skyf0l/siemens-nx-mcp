"""Public signatures and contracts for authoring/review tools."""

from __future__ import annotations

READ_ONLY = {
    "nx_list_expressions",
}
NON_MODEL: set[str] = set()


def nx_list_expressions(name_contains: str | None = None, offset: int = 0, limit: int = 50):
    """Paginate work-part expressions, formulas, numeric values in expression units, editability and stored immediate dependencies. Names are case-sensitive. Non-numeric values are null. Conditional dependencies may be incomplete."""

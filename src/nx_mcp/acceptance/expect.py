"""Machine-checked ``expect`` assertions of acceptance steps (architecture.md 2).

An ``expect`` maps a dotted path into the step result (``data.origin.2``,
``error.code``) to a plain value (``equals``) or an operator object:

- ``{"equals": v}``;
- ``{"close": x, "tol": t}`` (alias ``approx``): numbers or equal-length vectors;
- ``{"count": n}``: length of a list, dict or string;
- ``{"count_kind": {"feature": 1}}``: rows of a list counted by their ``kind``;
- ``{"contains": v}``: list member, substring, or a dict subset;
- ``{"matches": "regex"}``: ``re.search`` on the text.

Every path is one check.
"""

from __future__ import annotations

import math
import re
from typing import Any

OPERATORS = ("equals", "close", "approx", "count", "count_kind", "contains", "matches")
_MISSING = object()


def lookup(payload: Any, path: str):
    """The value at a dotted ``path`` (list indices are numbers), or ``_MISSING``."""
    current = payload
    for key in path.split(".") if path else []:
        if isinstance(current, list) and key.lstrip("-").isdigit():
            index = int(key)
            if not -len(current) <= index < len(current):
                return _MISSING
            current = current[index]
        elif isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return _MISSING
    return current


def is_operator(rule: Any) -> bool:
    return (
        isinstance(rule, dict)
        and bool(set(rule) & set(OPERATORS))
        and set(rule)
        <= {
            *OPERATORS,
            "tol",
        }
    )


def _within(actual, wanted, tol) -> bool:
    if isinstance(wanted, (list, tuple)):
        return (
            isinstance(actual, (list, tuple))
            and len(actual) == len(wanted)
            and all(_within(a, w, tol) for a, w in zip(actual, wanted, strict=True))
        )
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    return math.isfinite(actual) and abs(actual - wanted) <= tol


def _contains(actual, wanted) -> bool:
    if isinstance(actual, str):
        return isinstance(wanted, str) and wanted in actual
    if isinstance(actual, list):
        return any(
            item == wanted or (isinstance(wanted, dict) and _subset(item, wanted))
            for item in actual
        )
    if isinstance(actual, dict) and isinstance(wanted, dict):
        return _subset(actual, wanted)
    return False


def _subset(actual, wanted) -> bool:
    return isinstance(actual, dict) and all(
        k in actual and (actual[k] == v or _subset(actual[k], v)) for k, v in wanted.items()
    )


def check(payload: Any, path: str, rule: Any) -> str | None:
    """A failure message, or None when the assertion holds."""
    actual = lookup(payload, path)
    if actual is _MISSING:
        return f"{path}: missing (wanted {rule!r})"
    if not is_operator(rule):
        rule = {"equals": rule}
    for op, wanted in rule.items():
        if op == "tol":
            continue
        if op == "equals":
            # true never equals 1: booleans compare by identity.
            if isinstance(wanted, bool) or isinstance(actual, bool):
                ok = actual is wanted
            else:
                ok = actual == wanted
        elif op in ("close", "approx"):
            ok = _within(actual, wanted, float(rule.get("tol", 1e-9)))
        elif op == "count":
            ok = isinstance(actual, (list, dict, str)) and len(actual) == wanted
        elif op == "count_kind":
            counts: dict[str, int] = {}
            for row in actual if isinstance(actual, list) else []:
                kind = row.get("kind") if isinstance(row, dict) else None
                counts[kind] = counts.get(kind, 0) + 1
            ok = isinstance(actual, list) and all(
                counts.get(kind, 0) == n for kind, n in wanted.items()
            )
        elif op == "contains":
            ok = _contains(actual, wanted)
        elif op == "matches":
            ok = isinstance(actual, str) and re.search(wanted, actual) is not None
        else:  # pragma: no cover - is_operator() admits only known operators
            ok = False
        if not ok:
            return f"{path}: {op} {wanted!r} failed (actual {_short(actual)})"
    return None


def evaluate(payload: Any, expect: dict) -> list[str]:
    """Failure messages of every assertion in ``expect`` (empty = all pass)."""
    failures = []
    for path, rule in expect.items():
        message = check(payload, path, rule)
        if message:
            failures.append(message)
    return failures


def _short(value, limit: int = 160) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "â€¦"

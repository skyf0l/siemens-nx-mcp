"""What a drawing dimension shows: appended text and the composed callout text.

NX 2406 findings encoded here:

- ``Dimension.GetDimensionText()`` gives the nominal value only (``'20'`` for a fit
  dimension that shows ``Ø20 H7 +0.021/0``): the shown callout is composed from the
  diameter or radius symbol, the nominal, the tolerance or fit and the appended text.
- Appended text (``REF``, ``2X``, ``TYP``) is read and written as one ``AppendedText``
  object (``Dimension.GetAppendedText`` / ``SetAppendedText``), four line lists: before,
  after, above and below the value.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from nx_mcp.core.types import Text, forbid_extra

SIDES = ("before", "after", "above", "below")
SYMBOLS = {"DiameterDimension": "Ø", "RadiusDimension": "R", "FoldedRadiusDimension": "R"}


class Appended(TypedDict, total=False):
    before: Annotated[str, Text(32)]
    after: Annotated[str, Text(32)]


forbid_extra(Appended)
APPENDED_DOC = "e.g. {before: '2X', after: 'REF'}; '' clears"


def read_appended(d) -> dict:
    """``{side: [lines]}`` of the sides that hold text."""
    native = d.GetAppendedText()
    try:
        out = {}
        for side in SIDES:
            lines = [str(t) for t in getattr(native, f"Get{side.capitalize()}Text")() or []]
            if any(line.strip() for line in lines):
                out[side] = lines
        return out
    finally:
        native.Dispose()


def write_appended(d, wanted) -> None:
    """Replace the given sides' text, one line each ('' clears; others are kept)."""
    native = d.GetAppendedText()
    try:
        for side, text in wanted.items():
            getattr(native, f"Set{side.capitalize()}Text")([text] if text else [])
        d.SetAppendedText(native)
    finally:
        native.Dispose()


def _fixed(value, decimals, sign=False):
    text = f"{float(value):{'+' if sign else ''}.{max(int(decimals), 0)}f}"
    return "0" if float(text) == 0 else text


def tolerance_text(nominal, value, tol) -> tuple[str, str]:
    """``(value text, suffix)`` a tolerance shows around the nominal text."""
    kind = tol.get("type")
    decimals = tol.get("decimals", 2)
    if kind == "basic":
        return f"[{nominal}]", ""
    if kind == "reference":
        return f"({nominal})", ""
    if kind == "symmetric" and "upper" in tol:
        return nominal, f" ±{_fixed(abs(tol['upper']), decimals)}"
    if kind == "bilateral" and "upper" in tol:
        return nominal, (f" {_fixed(tol['upper'], decimals, True)}/"
                         f"{_fixed(tol['lower'], decimals, True)}")  # fmt: skip
    if kind == "limits" and "upper" in tol:
        return (f"{_fixed(value + tol['upper'], decimals)}/"
                f"{_fixed(value + tol['lower'], decimals)}"), ""  # fmt: skip
    if kind == "fit":
        classes = "/".join(tol[k] for k in ("hole", "shaft") if k in tol)
        display = tol.get("display", "symbol")
        if display == "deviations" and "upper" in tol:
            return nominal, f" {_fixed(tol['upper'], 3, True)}/{_fixed(tol['lower'], 3, True)}"
        if display == "symbol_and_deviations" and "upper" in tol:
            return nominal, (f" {classes} ({_fixed(tol['upper'], 3, True)}/"
                             f"{_fixed(tol['lower'], 3, True)})")  # fmt: skip
        return nominal, f" {classes}"
    return nominal, ""


def display_text(info) -> str | None:
    """The callout as the sheet shows it, composed from ``dimension_info`` fields:
    appended before, the diameter or radius symbol, the value with its tolerance or fit,
    appended after; the lines above and below are separate lines (one string, ``\\n``)."""
    nominal = info.get("text")
    if nominal is None:
        return None
    symbol = SYMBOLS.get(info.get("type", ""), "")
    value, suffix = tolerance_text(nominal, info.get("value", 0.0), info.get("tolerance", {}))
    appended = info.get("appended", {})
    line = " ".join(
        part
        for part in (
            " ".join(appended.get("before", [])),
            symbol + value + suffix,
            " ".join(appended.get("after", [])),
        )  # fmt: skip
        if part
    )
    return "\n".join([*appended.get("above", []), line, *appended.get("below", [])])

"""Appended text and the composed callout text of drawing dimensions."""

from nx_mcp.domains.drawing import dimension_text
from tests.kit import run
from tests.unit.drawing.test_dimension_kinds import fit_nx


class Appended:
    """NX's AppendedText: four line lists."""

    def __init__(self, store):
        self.store = store

    def Dispose(self):
        pass

    def __getattr__(self, name):
        side = name[3:-4].lower()  # GetBeforeText -> before
        if name.startswith("Get"):
            return lambda: list(self.store.get(side, []))
        return lambda lines: self.store.__setitem__(side, list(lines))


def with_appended(d, store=None):
    d.appended = {} if store is None else store
    d.GetAppendedText = lambda: Appended(dict(d.appended))
    d.SetAppendedText = lambda native: d.appended.update(native.store)
    d.GetDimensionText = lambda: (["16.0"], [])


def test_the_display_composes_symbol_value_tolerance_and_appended_text():
    info = {"type": "DiameterDimension", "text": "20", "value": 20.0,
            "tolerance": {"type": "fit", "hole": "H7", "display": "symbol_and_deviations",
                          "upper": 0.021, "lower": 0.0},
            "appended": {"before": ["2X"]}}  # fmt: skip
    assert dimension_text.display_text(info) == "2X Ø20 H7 (+0.021/0)"
    bilateral = {"type": "HorizontalDimension", "text": "100.00", "value": 100.0,
                 "tolerance": {"type": "bilateral", "upper": 0.1, "lower": -0.05, "decimals": 2},
                 "appended": {"after": ["REF"], "below": ["TYP"]}}  # fmt: skip
    assert dimension_text.display_text(bilateral) == "100.00 +0.10/-0.05 REF\nTYP"
    assert dimension_text.display_text({"type": "RadiusDimension", "text": "5",
                                        "tolerance": {"type": "basic"}}) == "R[5]"  # fmt: skip
    assert dimension_text.display_text({"value": 1.0}) is None


def test_appended_text_is_written_read_back_and_shown():
    nx, d, ref = fit_nx()
    with_appended(d)
    ok = run(nx, "nx_edit_dimension", dimension=ref, appended={"before": "2X", "after": "REF"})
    assert ok.ok, ok
    assert ok.data["appended"] == {"before": ["2X"], "after": ["REF"]}
    assert ok.data["display"] == "2X Ø16.0 REF"
    cleared = run(nx, "nx_edit_dimension", dimension=ref, appended={"before": ""})
    assert cleared.ok and cleared.data["appended"] == {"after": ["REF"]}
    got = run(nx, "nx_get_dimension", dimension=ref)
    assert got.data["display"] == "Ø16.0 REF" and got.data["text"] == "16.0"


def test_appended_text_nx_does_not_keep_fails_the_read_back():
    nx, d, ref = fit_nx()
    with_appended(d)
    d.SetAppendedText = lambda native: None  # NX ignores it
    bad = run(nx, "nx_edit_dimension", dimension=ref, appended={"after": "REF"})
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"


def test_new_text_on_a_dimension_points_to_appended_text():
    nx, d, ref = fit_nx()
    bad = run(nx, "nx_edit_annotation", annotation=ref, text=["100 REF"])
    assert bad.error["field"] == "/text" and "nx_edit_dimension appended" in bad.error["hint"], bad

"""Picks (coincident/concentric snaps, trim/extend) in a sketch of a part that is not
displayed: NX 2406 refuses that part's ModelingViews.WorkView, so the pick is made in one
of its modeling views (``common.pick_view``). A call that names the sketch of NX's quiet
work part beside another displayed part shows that part for the call (gate 4s,
core/switching.py): its picks are then made in the displayed part."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.sketch import common
from tests.kit import run
from tests.unit.sketch.fakes import P, sketch_nx


class BackgroundViews(list):
    @property
    def WorkView(self):
        raise RuntimeError("This operation can only be performed on a displayed part")


def _background(nx):
    """Show another part; the sketch's part keeps only its modeling views."""
    top = NS(Name="Top")
    nx.part.ModelingViews = BackgroundViews([top, NS(Name="Front")])
    nx.session.Parts.Display = NS(Tag=-1)
    return top


def _shown_elsewhere(nx):
    """Display another loaded part; the sketch's part stays NX's (quiet) work part."""
    from pathlib import Path

    from tests.kit.objects import Part

    nx.nx.DisplayPartOption = NS(AllowAdditional=1, ReplaceExisting=0)
    nx.nx.PartDisplayPartWorkPartOption = NS(SameAsDisplay=0, UseLast=1)
    parts = nx.session.Parts
    work = parts.Work
    other = Part(nx.session, Path(str(work.FullPath)).with_name("shown.prt"))
    parts.Work, parts.Display = work, other
    return other


def test_pick_view_is_the_work_view_of_the_displayed_part():
    nx = sketch_nx()
    assert common.pick_view(nx.tx, nx.part) is nx.part.ModelingViews.WorkView


def test_pick_view_of_a_background_part_is_its_first_modeling_view():
    nx = sketch_nx()
    top = _background(nx)
    assert common.pick_view(nx.tx, nx.part) is top


def test_coincident_and_concentric_pick_in_a_background_part():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    a = nx.line(sk, (0, 0, 0), (10, 0, 0))
    d = nx.line(sk, (12, 2, 0), (20, 8, 0))
    r1, r2 = nx.arc(sk, (40, 0, 0), 5, 0, 360), nx.arc(sk, (43, 2, 0), 2, 0, 360)
    other = _shown_elsewhere(nx)
    views = []
    real = nx.commit_relation

    def spy(kind, b):
        views.append((b.StationaryObject.SetValue.calls[-1][0][2], nx.session.Parts.Display))
        return real(kind, b)

    nx.on_commit("SketchMakeCoincidentBuilder", lambda b: spy("SketchMakeCoincidentBuilder", b))
    ok = run(nx, "nx_create_sketch_constraints", sketch=sk.ref, constraints=[
        {"type": "coincident", "points": [
            {"curve": nx.cref(a), "point": "end"}, {"curve": nx.cref(d), "point": "start"}]},
        {"type": "concentric", "curves": [nx.cref(r1), nx.cref(r2)]},
    ])  # fmt: skip
    assert ok.ok, ok
    shown = (nx.part.ModelingViews.WorkView, nx.part)  # the sketch's part is shown for it
    assert views == [shown, shown]  # the stationary pick of each relation
    assert nx.session.Parts.Display is other and nx.session.Parts.Work is nx.part
    assert (d.StartPoint.X, d.StartPoint.Y) == (10, 0)
    assert (r2.CenterPoint.X, r2.CenterPoint.Y) == (40, 0)


def test_trim_and_extend_pick_in_a_background_part():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    boundary = nx.line(sk, (0, -5, 0), (0, 5, 0))
    cut = nx.line(sk, (-5, 0, 0), (5, 0, 0))
    short = nx.line(sk, (-5, 3, 0), (-2, 3, 0))
    other = _shown_elsewhere(nx)
    views = []

    def trim(b):
        views.append((b.TrimmedCurves.Add.calls[-1][0][1], nx.session.Parts.Display))
        cut.StartPoint = P([0, 0, 0])

    def extend(b):
        views.append((b.ExtendedCurves.Add.calls[-1][0][1], nx.session.Parts.Display))
        short.EndPoint = P([0, 3, 0])

    nx.on_commit("QuickTrimBuilder", trim)
    nx.on_commit("QuickExtendBuilder", extend)
    ok = run(nx, "nx_edit_sketch_curves", sketch=sk.ref, edits=[
        {"type": "trim", "curve": nx.cref(cut), "boundaries": [nx.cref(boundary)], "pick": [-3, 0]},
        {"type": "extend", "curve": nx.cref(short), "boundaries": [nx.cref(boundary)],
         "pick": [-2, 3]},
    ])  # fmt: skip
    assert ok.ok, ok
    shown = (nx.part.ModelingViews.WorkView, nx.part)
    assert views == [shown, shown]
    assert nx.session.Parts.Display is other and nx.session.Parts.Work is nx.part
    assert ok.data["edits"][0]["curves"][0]["start"] == pytest.approx([0, 0])
    assert ok.data["edits"][1]["curves"][0]["end"] == pytest.approx([0, 3])

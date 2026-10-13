"""nx_set_view_settings: thread, edge and silhouette display of drawing views."""

from types import SimpleNamespace as NS

from tests.kit import run
from tests.unit.drawing.fakes import drawing_nx

DEFAULTS = {
    "ViewStyleThreads": {"StandardData": 4, "MinimumPitchFieldData": 0.0, "TrueHiddenLine": True},
    "ViewStyleHiddenLines": {"HiddenLine": True, "EdgesHiddenByEdges": False},
    "ViewStyleSmoothEdges": {"SmoothEdge": True},
    "ViewStyleVirtualIntersections": {"VirtualIntersections": False, "AdjacentBlends": False},
    "ViewStyleGeneral": {"Silhouettes": True, "UVGrid": True},
}


class SettingsBuilder:
    """The view's View Style groups; Commit stores them on the view (``keep`` refuses)."""

    def __init__(self, nx, views):
        self.nx, self.view = nx, views[0]
        stored = getattr(self.view, "settings", None) or DEFAULTS
        self.ViewStyle = NS(**{g: NS(**dict(v)) for g, v in stored.items()})
        nx.settings_builders.append(self)

    def InheritSettingsFromSelectedObjects(self, view):
        assert view is self.view

    def Commit(self):
        groups = {g: dict(vars(getattr(self.ViewStyle, g))) for g in DEFAULTS}
        for group, prop in self.nx.keep:  # NX ignores these values
            groups[group][prop] = (getattr(self.view, "settings", None) or DEFAULTS)[group][prop]
        self.view.settings = groups
        self.nx.commits.append(self.view.Name)

    def Destroy(self):
        self.destroyed = True


def settings_nx():
    nx = drawing_nx()
    sheet = nx.make_sheet()
    top = nx.make_view(sheet, "Top@1")
    front = nx.make_view(sheet, "ORTHO@2", pos=(100.0, 50.0), native="ProjectedView")
    nx.settings_builders, nx.keep, nx.commits = [], {}, []
    nx.part.SettingsManager = NS(
        CreateDrawingEditViewSettingsBuilder=lambda views: SettingsBuilder(nx, views)
    )
    nx.session.UpdateManager = NS(DoUpdate=lambda mark: 0)
    return nx, top, front


def test_settings_are_committed_per_view_and_read_back():
    nx, top, front = settings_nx()
    ok = run(nx, "nx_set_view_settings", views=[top.ref, "ortho@2"], settings={
        "threads": "schematic", "thread_min_pitch": 0.5, "smooth_edges": False,
        "virtual_intersections": True, "silhouettes": False})  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    rows = ok.data["views"]
    assert [r["id"] for r in rows] == [top.ref, front.ref]
    for row in rows:
        assert row["settings"]["threads"] == "schematic"
        assert row["settings"]["thread_min_pitch"] == 0.5
        assert row["settings"]["smooth_edges"] is False and row["settings"]["silhouettes"] is False
        assert row["settings"]["uv_grid"] is True  # left out: kept
    assert top.settings["ViewStyleThreads"]["StandardData"] == 2
    assert nx.commits == ["Top@1", "ORTHO@2"]  # one builder per view
    assert all(getattr(b, "destroyed", False) for b in nx.settings_builders)
    assert {r["id"] for r in ok.changes["modified"]} == {top.ref, front.ref}


def test_one_view_twice_is_set_once_and_thread_names_map_both_ways():
    nx, top, _ = settings_nx()
    ok = run(nx, "nx_set_view_settings", views=[top.ref, "top"],
             settings={"threads": "simplified_three_quarter"})  # fmt: skip
    assert ok.ok and len(ok.data["views"]) == 1 and nx.commits == ["Top@1"]
    assert top.settings["ViewStyleThreads"]["StandardData"] == 4
    for name, native in (("none", 0), ("simplified", 1), ("detailed", 3)):
        assert run(nx, "nx_set_view_settings", views=[top.ref], settings={"threads": name}).ok
        assert top.settings["ViewStyleThreads"]["StandardData"] == native


def test_a_value_nx_keeps_is_rolled_back_and_bad_calls_refused():
    nx, top, _ = settings_nx()
    nx.keep = {("ViewStyleGeneral", "UVGrid"): True}
    bad = run(nx, "nx_set_view_settings", views=[top.ref], settings={"uv_grid": False})
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"
    empty = run(nx, "nx_set_view_settings", views=[top.ref], settings={})
    assert empty.error["field"] == "/settings" and empty.mutation_outcome == "not_started"
    unknown = run(nx, "nx_set_view_settings", views=[top.ref], settings={"fonts": 1})
    assert unknown.error["field"].startswith("/settings")
    missing = run(nx, "nx_set_view_settings", views=["nope"], settings={"uv_grid": True})
    assert missing.error["field"] == "/views/0"
    negative = run(nx, "nx_set_view_settings", views=[top.ref],
                   settings={"thread_min_pitch": -1})  # fmt: skip
    assert negative.error["field"] == "/settings/thread_min_pitch"

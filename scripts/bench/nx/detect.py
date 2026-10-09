# 4. Change-detection candidates: cost on the current work part, and whether
# each one notices a simulated engineer change (made here, outside any bridge
# tool, the way an NX command would: visible undo mark + model change).
import json
import time

import NXOpen.Features as F
import NXOpen.UF as UF

import nx_mcp.interactive as I

h = I._host
x = h.executor
uf = UF.UFSession.GetUFSession()
V = NXOpen.Session.MarkVisibility
UV = UF.Undo.UserVisibility
uv_names = [n for n in dir(UV) if not n.startswith("_") and n[0].isupper() and n != "ValueOf"]
UV_ALL = UV.ANY_VIS
N = BENCH_ARGS.get("n", 200)
scenarios = BENCH_ARGS.get("scenarios", False)
pc = time.perf_counter


def c_newest_mark():
    return session.GetNewestUndoMark(V.AnyVisibility)


def c_mark_count():
    return uf.Undo.AskNumberOfMarks(UV_ALL)


def c_mark_id_count():
    return (session.GetNewestUndoMark(V.AnyVisibility), uf.Undo.AskNumberOfMarks(UV_ALL))


def c_all_marks():
    return tuple(m.Id for m in session.GetAllUndoMarks(V.AnyVisibility))


def c_parts_modified():
    s = session.Parts
    return (
        tuple((int(p.Tag), bool(p.IsModified)) for p in s),
        int(s.Work.Tag) if s.Work else 0,
        int(s.Display.Tag) if s.Display else 0,
    )


def c_counts():
    w = session.Parts.Work
    return (len(list(w.Features)), len(list(w.Bodies))) if w else None


def c_feature_tags():
    w = session.Parts.Work
    return tuple(int(f.Tag) for f in w.Features) if w else None


def c_host_snapshot():
    w = session.Parts.Work
    return frozenset(x._snapshot(w)) if w else None


def c_combined():
    """Proposed: newest mark + mark count + part list/modified/work/display."""
    return (c_mark_id_count(), c_parts_modified())


CANDIDATES = {
    "newest undo mark id (GetNewestUndoMark)": c_newest_mark,
    "undo mark count (UF AskNumberOfMarks)": c_mark_count,
    "newest mark id + mark count": c_mark_id_count,
    "all undo mark ids (GetAllUndoMarks)": c_all_marks,
    "parts: tags + IsModified + work/display": c_parts_modified,
    "work part feature/body counts": c_counts,
    "work part feature tags": c_feature_tags,
    "executor._snapshot(work) (all object keys)": c_host_snapshot,
    "combined: mark id+count + parts/modified": c_combined,
}

cost = {}
for name, fn in CANDIDATES.items():
    xs = []
    n = N if "snapshot" not in name else max(50, N // 4)
    for _ in range(n):
        t = pc()
        fn()
        xs.append((pc() - t) * 1000)
    cost[name] = summarize(xs)

detect = {}
if scenarios:
    p = session.Parts.Work
    assert "bench_control" in p.FullPath.replace("\\", "/"), p.FullPath

    def mark(name):
        return session.SetUndoMark(V.Visible, name)

    def add_datum(z):
        d = p.Features.CreateDatumPlaneBuilder(F.Feature.Null)
        try:
            pl = d.GetPlane()
            pl.SetMethod(NXOpen.PlaneTypes.MethodType.Fixed)
            pl.Origin = NXOpen.Point3d(0.0, 0.0, z)
            pl.Normal = NXOpen.Vector3d(0.0, 0.0, 1.0)
            pl.Evaluate()
            return d.CommitFeature()
        finally:
            d.Destroy()

    block = next(f for f in p.Features if f.FeatureType == "BLOCK")
    height = next(e for e in block.GetExpressions() if e.RightHandSide.strip() not in ("10", "12"))

    def s_idle():
        pass

    def s_view():
        p.ModelingViews.WorkView.Fit()
        p.ModelingViews.WorkView.Rotate(
            NXOpen.Point3d(0.0, 0.0, 0.0), NXOpen.Vector3d(0.0, 0.0, 1.0), 15.0
        )

    def s_add_feature():
        mark("Datum Plane")
        add_datum(55.0)

    def s_edit_expression():
        m = mark("Edit Expression")
        height.RightHandSide = "7.5" if height.RightHandSide.strip() != "7.5" else "6.5"
        session.UpdateManager.DoUpdate(m)

    def s_undo():  # Edit > Undo / Ctrl+Z: undoes and removes the newest visible mark
        session.UndoLastNVisibleMarks(1)

    def s_undo_keep_mark():  # Undo List "undo to here": restores but keeps the mark
        m = mark("Edit Expression")
        height.RightHandSide = "7.25"
        session.UpdateManager.DoUpdate(m)
        global BASE_AFTER_EDIT
        BASE_AFTER_EDIT = {n: fn() for n, fn in CANDIDATES.items()}
        session.UndoToMark(m, None)

    def s_undo_then_redo_other():
        # Engineer undoes the last feature and makes a different one: counts unchanged.
        session.UndoToMark(session.NewestVisibleUndoMark, None)
        mark("Datum Plane")
        add_datum(77.0)

    def s_hide_body():
        mark("Hide")
        b = list(p.Bodies)[0]
        session.DisplayManager.BlankObjects([b])
        session.DisplayManager.UnblankObjects([b])

    def s_no_mark_edit():
        # A change with no undo mark (e.g. an engineer's own journal) - worst case.
        height.RightHandSide = "8.5" if height.RightHandSide.strip() != "8.5" else "9.5"
        session.UpdateManager.DoUpdate(session.NewestVisibleUndoMark)

    def s_switch_work_part():
        other = next((q for q in session.Parts if q.Tag != p.Tag), None)
        if other is not None:  # engineer switches the displayed part
            session.Parts.SetActiveDisplay(
                other,
                NXOpen.DisplayPartOption.AllowAdditional,
                NXOpen.PartDisplayPartWorkPartOption.UseLast,
            )

    for sname, change in [
        ("idle (no engineer action)", s_idle),
        ("view fit/rotate (no model change)", s_view),
        ("add datum plane (visible mark)", s_add_feature),
        ("edit expression, same feature count", s_edit_expression),
        ("undo last command (Ctrl+Z)", s_undo),
        ("undo-to-mark keeping the mark", s_undo_keep_mark),
        ("undo + different feature (same counts)", s_undo_then_redo_other),
        ("hide/show body (display edit)", s_hide_body),
        ("edit expression without undo mark", s_no_mark_edit),
        ("switch displayed part", s_switch_work_part),
    ]:
        before = {n: fn() for n, fn in CANDIDATES.items()}
        BASE_AFTER_EDIT = None
        change()
        if BASE_AFTER_EDIT is not None:  # compare against the state the agent last saw
            before = BASE_AFTER_EDIT
        after = {n: fn() for n, fn in CANDIDATES.items()}
        detect[sname] = {n: before[n] != after[n] for n in CANDIDATES}
        if session.Parts.Work.Tag != p.Tag:
            session.Parts.SetActiveDisplay(
                p,
                NXOpen.DisplayPartOption.AllowAdditional,
                NXOpen.PartDisplayPartWorkPartOption.UseLast,
            )

print(
    "JSON:"
    + json.dumps(
        {
            "work": session.Parts.Work.Leaf if session.Parts.Work else None,
            "uv": [str(UV_ALL)] + uv_names,
            "cost": cost,
            "detect": detect,
        }
    )
)

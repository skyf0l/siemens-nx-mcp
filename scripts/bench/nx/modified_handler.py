# 4b. Does PartCollection.AddPartModifiedHandler fire on every engineer change
# (including ones without an undo mark), and what does it cost during agent work?
import json
import time

import NXOpen.Features as F

p = session.Parts.Work
assert "bench_control" in p.FullPath.replace("\\", "/"), p.FullPath
V = NXOpen.Session.MarkVisibility
calls = []


def on_modified(part, *rest):
    calls.append(time.perf_counter())


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
res = {}
pc = time.perf_counter
# Baseline cost of 10 datum planes without a handler.
t = pc()
for i in range(10):
    add_datum(100.0 + i)
res["10 datum planes, no handler (ms)"] = (pc() - t) * 1000
handle = session.Parts.AddPartModifiedHandler(on_modified)
try:

    def count(label, fn):
        n0 = len(calls)
        fn()
        res[label] = len(calls) - n0

    count(
        "view fit/rotate",
        lambda: (
            p.ModelingViews.WorkView.Fit(),
            p.ModelingViews.WorkView.Rotate(
                NXOpen.Point3d(0.0, 0.0, 0.0), NXOpen.Vector3d(0.0, 0.0, 1.0), 10.0
            ),
        ),
    )

    def edit_no_mark():
        height.RightHandSide = "6.75" if height.RightHandSide.strip() != "6.75" else "6.25"
        session.UpdateManager.DoUpdate(session.NewestVisibleUndoMark)

    count("edit expression without undo mark", edit_no_mark)

    def add_with_mark():
        session.SetUndoMark(V.Visible, "Datum Plane")
        add_datum(150.0)

    count("add datum plane (with mark)", add_with_mark)
    count("Ctrl+Z", lambda: session.UndoLastNVisibleMarks(1))

    def hide():
        session.SetUndoMark(V.Visible, "Hide")
        b = list(p.Bodies)[0]
        session.DisplayManager.BlankObjects([b])
        session.DisplayManager.UnblankObjects([b])

    count("hide/show body", hide)
    count("no action", lambda: None)
    n0 = len(calls)
    t = pc()
    for i in range(10):
        add_datum(200.0 + i)
    res["10 datum planes, with handler (ms)"] = (pc() - t) * 1000
    res["handler calls during 10 datum planes"] = len(calls) - n0
finally:
    session.Parts.RemovePartModifiedHandler(handle)
print("JSON:" + json.dumps(res))

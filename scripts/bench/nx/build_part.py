# Adds `blocks` united blocks and `datums` datum planes to the work part
# (a disposable bench part under smoke/bench_control/).
import json

import NXOpen.Features as F

p = session.Parts.Work
assert "bench_control" in p.FullPath.replace("\\", "/"), p.FullPath
blocks = BENCH_ARGS.get("blocks", 1)
datums = BENCH_ARGS.get("datums", 0)
target = None
for i in range(blocks):
    b = p.Features.CreateBlockFeatureBuilder(F.Feature.Null)
    try:
        b.Type = F.BlockFeatureBuilder.Types.OriginAndEdgeLengths
        b.SetOriginAndLengths(
            NXOpen.Point3d(i * 8.0, (i % 3) * 4.0, 0.0), "10", "12", str(5 + i % 4)
        )
        if target is not None:
            b.BooleanOption.Type = NXOpen.GeometricUtilities.BooleanOperation.BooleanType.Unite
            b.BooleanOption.SetTargetBodies([target])
        feat = b.CommitFeature()
    finally:
        b.Destroy()
    if target is None:
        target = feat.GetBodies()[0]
for i in range(datums):
    d = p.Features.CreateDatumPlaneBuilder(F.Feature.Null)
    try:
        plane = d.GetPlane()
        plane.SetMethod(NXOpen.PlaneTypes.MethodType.Fixed)
        plane.SetUpdateOption(NXOpen.SmartObject.UpdateOption.WithinModeling)
        plane.Origin = NXOpen.Point3d(0.0, 0.0, 20.0 + i)
        plane.Normal = NXOpen.Vector3d(0.0, 0.0, 1.0)
        plane.Evaluate()
        d.CommitFeature()
    finally:
        d.Destroy()
body = list(p.Bodies)
print(
    "JSON:"
    + json.dumps(
        {
            "part": p.Leaf,
            "features": len(list(p.Features)),
            "bodies": len(body),
            "faces": sum(len(b.GetFaces()) for b in body),
            "edges": sum(len(b.GetEdges()) for b in body),
        }
    )
)

"""nx_create_hole, nx_create_thread and nx_list_thread_sizes on the shared fake."""

import math
from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.modeling import holes
from tests.kit import run
from tests.kit.objects import point
from tests.unit.modeling.fakes import feature, modeling_nx

TABLE = """<?xml version="1.0"?>
<Threads>
  <ThreadedHole Standard="Metric Coarse" Unit="Metric" Size="M8 x 1.25" Method="Cut"
    RadialEngage="0.75" TapDrillDia="6.8" MajorDiameter="8" MinorDiameter="6.647"
    Pitch="1.25" Angle="60" NumStarts="1" Callout="M8x1.25"/>
  <ThreadedHole Standard="Metric Coarse" Unit="Metric" Size="M8 x 1.25" Method="Cut"
    RadialEngage="0.6" TapDrillDia="7.2" MajorDiameter="8" MinorDiameter="6.9"
    Pitch="1.25" Angle="60" NumStarts="1" Callout="M8x1.25"/>
  <ThreadedHole Standard="Unified Coarse" Unit="English" Size="1/4-20" Method="Cut"
    RadialEngage="0.75" TapDrillDia="0.201" MajorDiameter="0.25" MinorDiameter="0.19"
    Pitch="0.05" Angle="60" NumStarts="1" Callout="1/4-20 UNC"/>
</Threads>
"""


@pytest.fixture
def table(tmp_path, monkeypatch):
    folder = tmp_path / "UGII" / "modeling_standards"
    folder.mkdir(parents=True)
    (folder / "NX_Thread_Standard.xml").write_text(TABLE, encoding="utf-8")
    monkeypatch.setenv("UGII_BASE_DIR", str(tmp_path))


def hole_nx():
    nx = modeling_nx()
    block = nx.block(60, 40, 20)
    top = block.face((0, 0, 1))
    B = nx.nx.Features.HolePackageBuilder
    stored = {}

    def commit(b):
        stored.update(
            type=b.Type,
            form=b.GeneralHoleForm,
            limit=b.HoleDepthLimitOption,
            depth_option=b.DepthOption,
            values={
                name: float(getattr(b, name).RightHandSide)
                for name in (
                    "GeneralSimpleHoleDiameter",
                    "GeneralSimpleHoleDepth",
                    "GeneralTipAngle",
                    "GeneralCounterboreDiameter",
                    "GeneralCounterboreDepth",
                    "GeneralCounterboreHoleDiameter",
                    "GeneralCounterboreHoleDepth",
                    "TapDrillDiameter",
                    "ThreadedHoleDepth",
                    "ThreadedTipAngle",
                    "ThreadDepth",
                )
                if isinstance(getattr(b, name).__dict__.get("RightHandSide"), str)
            },
            size=getattr(b, "ThreadSize", None),
        )
        points = b.HolePosition.AddToSection.calls[0][0][0][0][1]
        f = feature(nx, "HOLE PACKAGE", [block])
        f.GetOrigins = lambda: [p.Coordinates for p in points]
        f.GetDirections = lambda: [point(0, 0, -1) for _ in points]
        block.volume -= nx.removed
        return f

    def edit_builder(kind, *args):
        b = make(kind, *args)
        if args and args[0] is not None:  # the read-back builder of a committed hole
            object.__setattr__(b, "Type", stored["type"])
            object.__setattr__(b, "GeneralHoleForm", stored["form"])
            object.__setattr__(b, "HoleDepthLimitOption", stored["limit"])
            object.__setattr__(b, "DepthOption", stored["depth_option"])
            object.__setattr__(b, "ThreadSize", stored["size"])
            for name, value in stored["values"].items():
                object.__setattr__(b, name, NS(Value=value))
        return b

    make = nx.builder
    nx.builder = edit_builder
    nx.on_commit("HolePackageBuilder", commit)
    nx.removed = 100.0
    return nx, block, top, B


def test_simple_holes_configure_the_hole_package_and_read_back():
    nx, block, top, B = hole_nx()
    ok = run(
        nx, "nx_create_hole", face=top.ref, positions=[[10, 10, 20], [30, 10, 20]], form="simple",
        diameter=6, depth={"type": "distance", "value": 10},
    )  # fmt: skip
    assert ok.ok, ok
    b = next(x for x in nx.builders if x.kind == "HolePackageBuilder" and x.commits)
    assert b.Type is B.Types.GeneralHole and b.DepthOption is B.HoleDepthOptions.ToCylinderBottom
    assert b.BooleanOperation.SetTargetBodies.calls[0][0][0] == [block]
    assert ok.data["removed"] == 100 and ok.data["direction"] == [0, 0, -1]
    tip = 3 / math.tan(math.radians(59))
    assert ok.data["nominal"] == pytest.approx(2 * (math.pi * 9 * 10 + math.pi * 9 * tip / 3))
    assert nx.live_builders == 0


def test_hole_positions_must_be_inside_the_face_and_on_its_plane():
    nx, block, top, _ = hole_nx()
    above = run(nx, "nx_create_hole", face=top.ref, positions=[[10, 10, 21]], form="simple",
                diameter=6, depth={"type": "distance", "value": 10})  # fmt: skip
    # Checking a position needs the face: the call touched NX and reports rolled_back.
    assert above.error["field"] == "/positions/0" and above.mutation_outcome == "rolled_back"
    assert above.changes["references"] == "preserved" and nx.builders == []
    top.contains = lambda p: 2
    outside = run(nx, "nx_create_hole", face=top.ref, positions=[[10, 10, 20]], form="simple",
                  diameter=6, depth={"type": "distance", "value": 10})  # fmt: skip
    assert "outside" in outside.error["message"] and nx.live_builders == 0


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"form": "counterbore"}, "/counterbore"),
        ({"form": "simple", "countersink": {"diameter": 9}}, "/countersink"),
        ({"form": "counterbore", "counterbore": {"diameter": 5, "depth": 2}}, "/counterbore"),
        ({"form": "countersink", "countersink": {"diameter": 30, "angle": 90}}, "/countersink"),
        ({"form": "simple", "tip_angle": 180}, "/tip_angle"),
        ({"form": "simple", "depth": {"type": "until_face", "face": "fc:1"}}, "/depth"),
    ],
)
def test_hole_shape_errors_before_nx(extra, field):
    nx, block, top, _ = hole_nx()
    args = {"form": "simple", "diameter": 6, "depth": {"type": "distance", "value": 10}, **extra}
    bad = run(nx, "nx_create_hole", face=top.ref, positions=[[10, 10, 20]], **args)
    assert bad.error["field"] == field, bad
    assert bad.mutation_outcome == "not_started" and nx.builders == []


def test_threaded_hole_needs_a_catalog_tap_drill(table):
    nx, block, top, B = hole_nx()
    args = {"face": top.ref, "positions": [[10, 10, 20]], "form": "threaded",
            "depth": {"type": "distance", "value": 15}}  # fmt: skip
    bad = run(nx, "nx_create_hole", diameter=7, thread={"standard": "Metric Coarse",
              "size": "M8 x 1.25"}, **args)  # fmt: skip
    assert bad.error["field"] == "/diameter"
    assert bad.error["details"]["tap_drill_diameters"] == [6.8, 7.2]
    unknown = run(nx, "nx_create_hole", diameter=6.8, thread={"standard": "Metric Fine",
                  "size": "M8 x 1"}, **args)  # fmt: skip
    assert unknown.error["code"] == "NX_NOT_FOUND"
    inch = run(nx, "nx_create_hole", diameter=0.201, thread={"standard": "Unified Coarse",
               "size": "1/4-20"}, **args)  # fmt: skip
    assert inch.error["code"] == "NX_UNSUPPORTED_OPTION"
    ok = run(nx, "nx_create_hole", diameter=6.8, thread={"standard": "Metric Coarse",
             "size": "M8 x 1.25", "depth": 12}, **args)  # fmt: skip
    assert ok.ok, ok
    b = next(x for x in nx.builders if x.kind == "HolePackageBuilder" and x.commits)
    assert b.RadialEngageOption == "0.75" and b.ThreadDepth.RightHandSide == "12.0"


def test_hole_that_removes_nothing_rolls_back():
    nx, block, top, _ = hole_nx()
    nx.removed = 0.0
    bad = run(nx, "nx_create_hole", face=top.ref, positions=[[10, 10, 20]], form="simple",
              diameter=6, depth={"type": "through_all"})  # fmt: skip
    assert bad.error["details"]["check"] == "removed" and bad.mutation_outcome == "rolled_back"


def test_nominal_volume_of_counterbore_and_countersink():
    spec = {"form": "counterbore", "diameter": 6.0, "depth": 12.0, "tip_angle": 118.0,
            "counterbore": {"diameter": 11.0, "depth": 4.0}}  # fmt: skip
    assert holes.nominal_volume(spec) == pytest.approx(623.3163, abs=1e-4)
    spec = {"form": "countersink", "diameter": 6.0, "depth": 12.0, "tip_angle": 118.0,
            "countersink": {"diameter": 12.0, "angle": 90.0}}  # fmt: skip
    assert holes.nominal_volume(spec) == pytest.approx(469.3783, abs=1e-4)


def test_list_thread_sizes_levels(table):
    nx = modeling_nx()
    standards = run(nx, "nx_list_thread_sizes")
    assert [r["standard"] for r in standards.data["items"]] == ["Metric Coarse", "Unified Coarse"]
    sizes = run(nx, "nx_list_thread_sizes", standard="Metric Coarse")
    assert sizes.page["total"] == 2 and "pitch" not in sizes.data["items"][0]
    rows = run(nx, "nx_list_thread_sizes", standard="Metric Coarse", size="M8 x 1.25", limit=1)
    assert rows.data["items"][0]["tap_drill"] == 6.8 and rows.page["next_offset"] == 1
    bad = run(nx, "nx_list_thread_sizes", size="M8 x 1.25")
    assert bad.error["field"] == "/standard"


def thread_nx(detailed_cut=0.0):
    nx = modeling_nx()
    body = nx.block(20, 20, 30)
    cyl = nx.face(normal=(0, 0, 1), kind="cylinder", body=body)
    nx.remember(cyl)
    cyl.radius = 3.0
    start = body.face((0, 0, 1))

    def commit(b):
        for name, value in (("IsInternalThread", False), ("Pitch", 1.0), ("MajorDiameter", 6.0),
                            ("MinorDiameter", 4.917)):  # fmt: skip
            object.__setattr__(b, name, value)
        if b.ThreadInput is nx.nx.Features.ThreadBuilder.Input.Manual:
            object.__setattr__(b, "Pitch", float(b.PitchExp.RightHandSide))
            object.__setattr__(b, "MajorDiameter", float(b.MajorDiameterExp.RightHandSide))
            object.__setattr__(b, "MinorDiameter", float(b.MinorDiameterExp.RightHandSide))
        body.volume -= detailed_cut
        return feature(nx, "THREAD", [])

    nx.on_commit("ThreadBuilder", commit)
    return nx, body, cyl, start


def test_manual_symbolic_thread_keeps_volume_and_reads_back():
    nx, body, cyl, start = thread_nx()
    ok = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=10,
             spec={"pitch": 1, "major_diameter": 6, "minor_diameter": 4.9})  # fmt: skip
    assert ok.ok, ok
    b = nx.last("ThreadBuilder")
    assert b.ShaftDiameterExp.RightHandSide == "6.0" and b.NumStarts == 1
    assert ok.data["volume"] == body.volume and ok.data["minor_diameter"] == 4.9
    bad = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=10,
              spec={"pitch": 1, "major_diameter": 4, "minor_diameter": 5})  # fmt: skip
    assert bad.error["field"] == "/spec" and bad.mutation_outcome == "not_started"
    start = nx.ref_of(start, "face")
    flat = run(nx, "nx_create_thread", face=start, start_face=start, length=10,
               spec={"pitch": 1, "major_diameter": 6, "minor_diameter": 4.9})  # fmt: skip
    assert flat.error["code"] == "NX_WRONG_GEOMETRY"


def test_detailed_table_thread_must_cut(table):
    nx, body, cyl, start = thread_nx(detailed_cut=0.0)
    ok_args = {
        "face": cyl.ref,
        "start_face": start.ref,
        "length": 10,
        "spec": {"standard": "Metric Coarse", "size": "M8 x 1.25", "engagement": "0.75"},
    }
    bad = run(nx, "nx_create_thread", detailed=True, **ok_args)
    assert bad.error["details"]["check"] == "volume" and bad.mutation_outcome == "rolled_back"
    ambiguous = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=10,
                    spec={"standard": "Metric Coarse", "size": "M8 x 1.25"})  # fmt: skip
    assert ambiguous.error["code"] == "NX_AMBIGUOUS_REFERENCE"
    assert len(ambiguous.error["details"]["candidates"]) == 2

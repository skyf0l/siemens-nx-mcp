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
  <ThreadedHole Standard="Metric Coarse" Unit="Metric" Size="M6 x 1.0" Method="CUT"
    RadialEngage="Custom" TapDrillDia="5" MajorDiameter="6" MinorDiameter="4.891"
    Pitch="1" Angle="60" NumStarts="1" Callout="M6_x_1.0"/>
  <ThreadedHole Standard="Metric Coarse" Unit="Metric" Size="M6 x 1.0" Method="CUT"
    RadialEngage="0.5" TapDrillDia="5.2" MajorDiameter="6" MinorDiameter="4.891"
    Pitch="1" Angle="60" NumStarts="1" Callout="M6_x_1.0"/>
  <ThreadedHole Standard="Metric Coarse" Unit="Metric" Size="M6 x 1.0" Method="CUT"
    RadialEngage="0.75" TapDrillDia="5" MajorDiameter="6" MinorDiameter="4.891"
    Pitch="1" Angle="60" NumStarts="1" Callout="M6_x_1.0"/>
  <ThreadedHole Standard="Metric Fine" Unit="Metric" Size="M6 x 0.75" Method="CUT"
    RadialEngage="0.75" TapDrillDia="5.25" MajorDiameter="6" MinorDiameter="5.188"
    Pitch="0.75" Angle="60" NumStarts="1" Callout="M6_x_0.75"/>
  <ThreadedHole Standard="Metric Fine" Unit="Metric" Size="M12 x 1.5" Method="CUT"
    RadialEngage="0.75" TapDrillDia="10.5" MajorDiameter="12" MinorDiameter="10.376"
    Pitch="1.5" Angle="60" NumStarts="1" Callout="M12_x_1.5"/>
  <ThreadedHole Standard="Metric Fine" Unit="Metric" Size="M12 x 1.25" Method="CUT"
    RadialEngage="0.75" TapDrillDia="10.8" MajorDiameter="12" MinorDiameter="10.647"
    Pitch="1.25" Angle="60" NumStarts="1" Callout="M12_x_1.25"/>
  <ThreadedHole Standard="Inch UNC" Unit="Inch" Size="1/4-20" Method="CUT"
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
    # The thread table is read before NX is touched: nothing to roll back.
    assert unknown.error["code"] == "NX_NOT_FOUND" and unknown.mutation_outcome == "not_started"
    assert unknown.error["field"] == "/thread/size"
    inch = run(nx, "nx_create_hole", diameter=0.201, thread={"standard": "unc",
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
    assert [r["standard"] for r in standards.data["items"]] == [
        "Inch UNC",
        "Metric Coarse",
        "Metric Fine",
    ]
    # One row per size, its engagements listed (the table repeats a size per engagement).
    sizes = run(nx, "nx_list_thread_sizes", standard="metric coarse")
    assert sizes.page["total"] == 2 and "tap_drill" not in sizes.data["items"][0]
    m6 = next(r for r in sizes.data["items"] if r["size"] == "M6 x 1.0")
    assert m6["engagements"] == ["Custom", "0.5", "0.75"] and m6["major_diameter"] == 6
    rows = run(nx, "nx_list_thread_sizes", standard="Metric Coarse", size="M8 x 1.25", limit=1)
    assert rows.data["items"][0]["tap_drill"] == 6.8 and rows.page["next_offset"] == 1
    # A size alone: metric sizes are looked up in Metric Coarse, then Metric Fine.
    alone = run(nx, "nx_list_thread_sizes", size="M6")
    assert {r["size"] for r in alone.data["items"]} == {"M6 x 1.0"}
    fine = run(nx, "nx_list_thread_sizes", size="m6x0.75")
    assert fine.data["items"][0]["standard"] == "Metric Fine"


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
    cyl.radius = 4.0
    ok_args = {
        "face": cyl.ref,
        "start_face": start.ref,
        "length": 10,
        "spec": {"standard": "Metric Coarse", "size": "M8 x 1.25", "engagement": "0.75"},
    }
    bad = run(nx, "nx_create_thread", detailed=True, **ok_args)
    assert bad.error["details"]["check"] == "volume" and bad.mutation_outcome == "rolled_back"
    # No engagement: the 0.75 row is taken (the table offers 0.75 and 0.6 here).
    plain = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=10,
                spec={"standard": "Metric Coarse", "size": "M8 x 1.25"})  # fmt: skip
    assert plain.ok, plain
    assert plain.data["engagement"] == "0.75" and nx.last("ThreadBuilder").RadialEngage == "0.75"


# ---- natural argument forms ----------------------------------------------------------
def test_hole_short_forms_depth_number_default_form_and_2d_positions():
    nx, block, top, B = hole_nx()
    ok = run(nx, "nx_create_hole", face=top.ref, positions=[[10, 10], [30, 10]], diameter=6,
             depth="10 mm")  # fmt: skip
    assert ok.ok, ok
    b = committed(nx, "HolePackageBuilder")
    assert b.GeneralHoleForm is B.HoleForms.Simple
    assert b.GeneralSimpleHoleDepth.RightHandSide == "10.0"
    assert ok.data["positions"] == [[10, 10, 20], [30, 10, 20]]
    # A committed feature re-keys references (as NX does): reacquire the face.
    face = nx.ref_of(top, "face")
    through = run(nx, "nx_create_hole", face=face, positions=[[10, 30]], diameter=6,
                  depth="through_all")  # fmt: skip
    assert through.ok, through
    limit = committed(nx, "HolePackageBuilder").HoleDepthLimitOption
    assert limit is B.HoleDepthLimitOptions.ThroughBody
    assert through.data["nominal"] is None


def test_hole_errors_say_what_to_send():
    nx, block, top, _ = hole_nx()
    base = {"face": top.ref, "positions": [[10, 10, 20]], "depth": 10}
    cb = run(nx, "nx_create_hole", diameter=6, counterbore={"diameter": 11, "depth": 4}, **base)
    assert cb.error["field"] == "/counterbore" and "form 'counterbore'" in cb.error["hint"]
    assert cb.mutation_outcome == "not_started"
    no_d = run(nx, "nx_create_hole", **base)
    assert no_d.error["field"] == "/diameter" and no_d.mutation_outcome == "not_started"
    threaded = run(nx, "nx_create_hole", form="threaded", **base)
    assert threaded.error["field"] == "/thread" and "'M6'" in threaded.error["hint"]
    negative = run(nx, "nx_create_hole", face=top.ref, positions=[[10, 10]], diameter=6, depth=-3)
    assert negative.error["field"] == "/depth" and negative.mutation_outcome == "not_started"
    off = run(nx, "nx_create_hole", face=top.ref, positions=[[10, 10, 0]], diameter=6, depth=5)
    assert "z = 20" in off.error["message"] and off.error["field"] == "/positions/0"


def test_threaded_hole_from_a_bare_size_drills_the_tap_drill(table):
    nx, block, top, B = hole_nx()
    args = {"face": top.ref, "positions": [[10, 10]], "form": "threaded", "depth": 12}
    ok = run(nx, "nx_create_hole", thread={"size": "M6"}, **args)
    assert ok.ok, ok
    b = committed(nx, "HolePackageBuilder")
    assert b.Type is B.Types.ThreadedHole and b.ThreadStandard == "Metric Coarse"
    assert b.ThreadSize == "M6 x 1.0" and b.RadialEngageOption == "0.75"
    assert b.TapDrillDiameter.RightHandSide == "5.0"
    # A loose standard and size, and the 0.5-engagement tap drill as the diameter.
    args["face"] = nx.ref_of(top, "face")
    loose = run(nx, "nx_create_hole", diameter=5.2, thread={"standard": "ISO metric coarse",
                "size": "m6x1"}, **args)  # fmt: skip
    assert loose.ok, loose
    assert committed(nx, "HolePackageBuilder").RadialEngageOption == "0.5"
    # A clearance diameter is refused with the tap drills and what to do instead.
    args["face"] = nx.ref_of(top, "face")
    clear = run(nx, "nx_create_hole", diameter=6.6, thread={"size": "M6"}, **args)
    assert clear.error["field"] == "/diameter" and clear.mutation_outcome == "not_started"
    assert clear.error["details"]["tap_drill_diameters"] == [5.0, 5.2]
    assert "form 'simple'" in clear.error["hint"]


def test_thread_names_are_matched_loosely(table):
    from nx_mcp.domains.modeling import threads

    rows = threads.thread_rows()
    assert threads.resolve_standard(rows, "metric") == "Metric Coarse"
    assert threads.resolve_standard(rows, "METRIC FINE") == "Metric Fine"
    assert threads.resolve_standard(rows, "UNC") == "Inch UNC"
    assert threads.resolve_size(rows, None, "M6") == ("Metric Coarse", "M6 x 1.0")
    assert threads.resolve_size(rows, None, "M6 x 0.75") == ("Metric Fine", "M6 x 0.75")
    assert threads.resolve_size(rows, "Metric Coarse", "M6X1.0") == ("Metric Coarse", "M6 x 1.0")
    assert threads.resolve_size(rows, None, "1/4") == ("Inch UNC", "1/4-20")
    m6 = threads.size_rows(rows, None, "M6")
    assert threads.pick_row(m6)["RadialEngage"] == "0.75"
    assert threads.pick_row(m6, method="cut", engagement=0.5)["TapDrillDia"] == "5.2"


def test_thread_name_errors_offer_the_close_names(table):
    nx = modeling_nx()
    std = run(nx, "nx_list_thread_sizes", standard="Metric Corse")
    assert std.error["code"] == "NX_NOT_FOUND" and "Metric Coarse" in std.error["message"]
    many = run(nx, "nx_list_thread_sizes", standard="Metric Fine", size="M12")
    assert many.error["field"] == "/size"
    assert many.error["details"]["candidates"] == ["M12 x 1.5", "M12 x 1.25"]
    near = run(nx, "nx_list_thread_sizes", standard="Metric Coarse", size="M6 x 1.5")
    assert near.error["code"] == "NX_NOT_FOUND"
    assert near.error["details"]["close_matches"][0] == "M6 x 1.0"


def committed(nx, kind):
    """The last builder of ``kind`` that committed (not a read-back edit builder)."""
    return [x for x in nx.builders if x.kind == kind and x.commits][-1]


def end_faces(nx, cyl, *ends, far=10.0):
    """Give ``cyl`` circular edges bounded by the planar ``ends`` (and a far rim at z=far)."""
    edges = []
    for end in ends:
        e = nx.edge((3, 0, end.origin[2]), (3, 0, end.origin[2]))
        e.GetFaces = lambda end=end: [cyl, end]
        end.GetEdges = lambda e=e: [e]  # a cap within the radius: both ends free
        edges.append(e)
    rim = nx.edge((3, 0, far), (3, 0, far))
    rim.GetFaces = lambda: [cyl]
    cyl.GetEdges = lambda: [*edges, rim]


def test_thread_from_a_bare_size_finds_its_start_face_and_length(table):
    nx, body, cyl, start = thread_nx()
    end_faces(nx, cyl, start)
    ok = run(nx, "nx_create_thread", face=cyl.ref, spec={"size": "M6", "engagement": 0.75})
    assert ok.ok, ok
    b = nx.last("ThreadBuilder")
    assert b.StartObject.Value is start and b.ThreadLength.RightHandSide == "20.0"
    assert b.ThreadStandard == "Metric Coarse" and b.ThreadSize == "M6 x 1.0"
    assert ok.data["length"] == 20 and ok.data["engagement"] == "0.75"
    # Two planar ends: the call names both so the next call can pick one.
    bottom = body.face((0, 0, -1))
    end_faces(nx, cyl, start, bottom)
    two = run(nx, "nx_create_thread", face=nx.ref_of(cyl, "face"), spec={"size": "M6"}, length=5)
    assert two.error["field"] == "/start_face" and len(two.error["details"]["candidates"]) == 2


def test_thread_refuses_a_bore_without_material_to_thread(table):
    nx, body, cyl, start = thread_nx()
    ask = nx.uf.Modeling.AskFaceData

    def bore(tag):  # NX 2406 reports a hole's cylinder with a reversed normal (-1)
        data = list(ask(tag))
        return (*data[:6], -1) if tag == cyl.Tag else ask(tag)

    nx.uf.Modeling.AskFaceData = bore
    # An M6 thread in a 6 mm bore (or a 6.6 clearance hole): nothing to thread.
    bad = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=8,
              spec={"size": "M6"})  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and "tap drill 5" in bad.error["hint"]
    assert "clearance hole for M6 x 1.0" in bad.error["message"]
    assert "nx_edit_feature" in bad.error["hint"] and "larger" not in bad.error["hint"]
    assert bad.error["details"]["tap_drill"] == 5
    assert not [b for b in nx.builders if b.kind == "ThreadBuilder"]
    # An external M8 on a 6 mm shaft (below its minor diameter): refused too.
    nx.uf.Modeling.AskFaceData = ask
    shaft = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=8,
                spec={"size": "M8"})  # fmt: skip
    assert shaft.error["code"] == "NX_WRONG_GEOMETRY" and "minor" in shaft.error["message"]


def test_thread_build_failure_explains_the_nx_refusal(table):
    nx, body, cyl, start = thread_nx()
    nx.refuse("ThreadBuilder", code=671154, message="Invalid thread axis direction.")
    bad = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=8,
              spec={"size": "M6"}, reverse=True)  # fmt: skip
    assert bad.error["nx_code"] == 671154 and "reverse false" in bad.error["hint"]
    assert bad.mutation_outcome == "rolled_back"


def test_thread_spec_without_size_says_what_to_send(table):
    nx, body, cyl, start = thread_nx()
    bad = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=8,
              spec={"engagement": "0.75"})  # fmt: skip
    assert bad.error["field"] == "/spec/size" and bad.mutation_outcome == "not_started"
    eng = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=8,
              spec={"size": "M6", "engagement": 0.6})  # fmt: skip
    assert eng.error["field"] == "/spec/engagement" and "'0.75'" in eng.error["hint"]


def test_custom_internal_spec_off_the_table_or_the_bore_is_noted(table):
    nx, body, cyl, start = thread_nx()
    ask = nx.uf.Modeling.AskFaceData
    nx.uf.Modeling.AskFaceData = lambda tag: (
        (*list(ask(tag))[:6], -1) if tag == cyl.Tag else ask(tag)
    )
    cyl.radius = 3.3  # the 6.6 clearance bore of the benchmark: major 7 / minor 6.6 on it
    odd = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=8,
              spec={"pitch": 1, "major_diameter": 7, "minor_diameter": 6.6})  # fmt: skip
    assert odd.ok, odd
    notes = [w["message"] for w in odd.warnings if w["code"] == "NX_NOTE"]
    assert any("Non-standard thread" in n for n in notes), odd.warnings
    # A table-matching custom spec on its tap-drill bore: no note.
    cyl.radius = 2.5
    fine = run(nx, "nx_create_thread", face=nx.ref_of(cyl, "face"),
               start_face=nx.ref_of(start, "face"), length=8,
               spec={"pitch": 1, "major_diameter": 6, "minor_diameter": 4.917})  # fmt: skip
    assert fine.ok and not fine.warnings, fine.warnings
    # A minor diameter far below the bore: the thread does not fit it.
    cyl.radius = 2.9
    off = run(nx, "nx_create_thread", face=nx.ref_of(cyl, "face"),
              start_face=nx.ref_of(start, "face"), length=8,
              spec={"pitch": 1, "major_diameter": 6, "minor_diameter": 4.9})  # fmt: skip
    assert any("does not fit" in w["message"] for w in off.warnings), off.warnings
    # Major and pitch of M6 x 1.0 with a minor diameter of no row: non-standard too.
    cyl.radius = 2.8
    shallow = run(nx, "nx_create_thread", face=nx.ref_of(cyl, "face"),
                  start_face=nx.ref_of(start, "face"), length=8,
                  spec={"pitch": 1, "major_diameter": 6, "minor_diameter": 5.6})  # fmt: skip
    notes = [w["message"] for w in shallow.warnings]
    assert any("M6 x 1.0 has minor diameter 4.891" in n for n in notes), notes


def bolt_nx(radius=3.0, head=5.0):
    """A shaft of ``radius`` from z=0 (its free tip cap) to z=20, where it meets the
    underside of a ``head``-radius cylinder (a shoulder reaching beyond the shaft)."""
    nx, body, _cyl, _start = thread_nx()
    shaft = nx.remember(nx.face(normal=(0, 0, 1), kind="cylinder", body=body))
    shaft.radius = radius
    tip = nx.face(normal=(0, 0, -1), origin=(0, 0, 0), body=body)
    shoulder = nx.face(normal=(0, 0, 1), origin=(0, 0, 20), body=body)

    def ring(r, z, *faces):
        edge = nx.edge((r, 0, z), (r, 0, z), body=body, linear=False)
        edge.GetFaces = lambda: list(faces)
        return edge

    tip_edge, root_edge = ring(radius, 0, shaft, tip), ring(radius, 20, shaft, shoulder)
    shaft.GetEdges = lambda: [tip_edge, root_edge]
    tip.GetEdges = lambda: [tip_edge]
    shoulder.GetEdges = lambda: [root_edge, ring(head, 20, shoulder)]
    return nx, shaft, tip, shoulder


MANUAL_M6 = {"pitch": 1, "major_diameter": 6, "minor_diameter": 4.9}


def test_thread_starts_at_the_free_end_without_start_face():
    # A bolt: the shaft's tip is a cap within its radius, the head's underside reaches
    # beyond it, so the thread starts at the tip and runs to the head.
    nx, shaft, tip, shoulder = bolt_nx()
    ok = run(nx, "nx_create_thread", face=shaft.ref, spec=MANUAL_M6)
    assert ok.ok, ok
    assert nx.last("ThreadBuilder").StartObject.Value is tip
    assert ok.data["start_face"]["id"] == tip.ref and ok.data["length"] == 20
    # Both ends free (a plain rod) or both meeting larger faces: still ambiguous.
    rod, rod_shaft, *_ = bolt_nx(head=3.0)
    both = run(rod, "nx_create_thread", face=rod_shaft.ref, spec=MANUAL_M6)
    assert both.error["field"] == "/start_face" and len(both.error["details"]["candidates"]) == 2
    assert "both free" in both.error["message"]


def test_bore_thread_starts_at_its_opening_not_its_flat_floor():
    # The same faces as a blind bore (face sense reversed): the cap is its flat floor and
    # the face reaching beyond the radius is the opening the tap enters from.
    nx, bore, floor, opening = bolt_nx(radius=2.5)
    plain = nx.uf.Modeling.AskFaceData

    def reversed_bore(tag):
        data = plain(tag)
        return (*data[:6], -1) if tag == bore.Tag else data

    nx.uf.Modeling.AskFaceData = reversed_bore
    ok = run(nx, "nx_create_thread", face=bore.ref, spec=MANUAL_M6)
    assert ok.ok, ok
    assert nx.last("ThreadBuilder").StartObject.Value is opening
    assert ok.data["start_face"]["id"] == opening.ref

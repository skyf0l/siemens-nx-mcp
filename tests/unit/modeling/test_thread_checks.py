"""nx_create_thread argument checks before NX: length within the face, units before fit."""

from tests.kit import run
from tests.unit.modeling.fakes import modeling_nx
from tests.unit.modeling.test_holes_threads import end_faces, table, thread_nx  # noqa: F401


def test_thread_length_past_the_face_is_refused_before_nx(table):  # noqa: F811
    nx, _body, cyl, start = thread_nx()
    end_faces(nx, cyl, start)  # the face runs 20 from start_face
    bad = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=25,
              spec={"size": "M6"})  # fmt: skip
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/length"
    assert bad.error["details"]["face_length"] == 20 and bad.mutation_outcome == "not_started"
    assert not [b for b in nx.builders if b.kind == "ThreadBuilder"]
    ok = run(nx, "nx_create_thread", face=nx.ref_of(cyl, "face"),
             start_face=nx.ref_of(start, "face"), length=20, spec={"size": "M6"})  # fmt: skip
    assert ok.ok, ok


def test_metric_size_on_an_inch_part_reports_the_units_first(table):  # noqa: F811
    nx = modeling_nx(units="in")
    body = nx.block(1, 1, 2)
    cyl = nx.face(normal=(0, 0, 1), kind="cylinder", body=body)
    nx.remember(cyl)
    cyl.radius = 0.25  # a 0.5 in shaft: smaller than M8's minor diameter in mm
    start = body.face((0, 0, 1))
    bad = run(nx, "nx_create_thread", face=cyl.ref, start_face=start.ref, length=1,
              spec={"size": "M8"})  # fmt: skip
    assert bad.error["code"] == "NX_UNSUPPORTED_OPTION", bad
    assert bad.error["field"] == "/spec/standard" and bad.mutation_outcome == "not_started"

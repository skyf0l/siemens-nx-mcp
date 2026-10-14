from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import sketch
from tests.kit import fake_nx


def test_frame_and_conversions():
    nx = fake_nx()
    s = nx.sketch(origin=(0, 0, 5), x_axis=(0, 1, 0), y_axis=(0, 0, 1))
    f = sketch.frame(s)
    assert f["normal"] == [1.0, 0.0, 0.0]
    p = sketch.to_part(f, [2, 3])
    assert p == [0.0, 2.0, 8.0]
    assert sketch.to_local(f, p) == [2.0, 3.0]
    assert sketch.to_local(f, NS(X=0, Y=2, Z=8)) == [2.0, 3.0]
    assert sketch.height(f, [4, 0, 5]) == 4.0
    assert sketch.to_part_direction(f, [2, 3]) == [0.0, 2.0, 3.0]  # no origin shift


def test_editing_activates_and_restores():
    nx = fake_nx()
    s = nx.sketch()
    with sketch.editing(nx.tx, s.ref) as sk:
        assert nx.session.ActiveSketch is s and sk.activated
        assert (sk.point([1, 2]).X, sk.point([1, 2]).Y) == (1.0, 2.0)
    assert nx.session.ActiveSketch is None
    with pytest.raises(RuntimeError), sketch.editing(nx.tx, s):
        raise RuntimeError("boom")
    assert nx.session.ActiveSketch is None  # restored after a failure too


def test_editing_keeps_an_already_active_sketch_and_rejects_another():
    nx = fake_nx()
    s, other = nx.sketch(), nx.sketch()
    s.Activate()
    with sketch.editing(nx.tx, s) as sk:
        assert not sk.activated
    assert nx.session.ActiveSketch is s
    with pytest.raises(NXError) as error, sketch.editing(nx.tx, other):
        pass
    assert error.value.code == "NX_SKETCH_ACTIVE"
    foreign = nx.sketch()
    foreign.OwningPart = object()
    s.Deactivate()
    with pytest.raises(NXError) as error, sketch.editing(nx.tx, foreign):
        pass
    assert error.value.code == "NX_WRONG_OWNER"


def test_update_uses_local_update_only_for_the_active_sketch():
    nx = fake_nx()
    s = nx.sketch()
    sketch.update(nx.tx, s)
    assert s.updates == ["Update"]
    with sketch.editing(nx.tx, s) as sk:
        sketch.update(nx.tx, sk)
    assert s.updates == ["Update", "LocalUpdate", "Update"]


def _dimension(exp, measured):
    return NS(AssociatedExpression=exp, AssociatedDimension=NS(ComputedSize=measured))


def test_solve_checks_edited_dimensions_and_status():
    nx = fake_nx()
    s = nx.sketch()
    exp = nx.expression("p1", "20", value=20.0)
    s.constraints = [_dimension(exp, 20.0)]
    s.status = (2, 0)  # WellConstrained
    assert sketch.solve(nx.tx, s, [exp]) == {"status": "WellConstrained", "dof": 0, "native_dof": 0}
    assert nx.undo_marks == []  # the work-region change was undone
    assert nx.last("WorkRegionBuilder").destroyed

    s.constraints = [_dimension(exp, 19.0)]
    with pytest.raises(NXError) as error:
        sketch.solve(nx.tx, s, [exp])
    assert error.value.code == "NX_CONSTRAINT_UNSATISFIED"
    assert error.value.details["measured"] == 19.0

    s.constraints, s.status = [], (3, 2)
    with pytest.raises(NXError) as error:
        sketch.solve(nx.tx, s)
    assert error.value.code == "NX_SKETCH_OVERCONSTRAINED"
    assert error.value.category == "solver_failed"


def test_solve_follows_dependent_expressions():
    nx = fake_nx()
    s = nx.sketch()
    base, dependent = nx.expression("a", "10", 10.0), nx.expression("b", "a*2", 20.0)
    base.dependents = [dependent]
    s.constraints = [_dimension(dependent, 21.0)]
    with pytest.raises(NXError):
        sketch.solve(nx.tx, s, [base])


def test_owns():
    nx = fake_nx()
    s = nx.sketch()
    mine, stray = nx.curve(s), nx.curve()
    assert sketch.owns(nx.tx, s, mine.ref) is mine
    with pytest.raises(NXError) as error:
        sketch.owns(nx.tx, s, stray)
    assert error.value.code == "NX_WRONG_OWNER" and error.value.field == "/curve"


# ---- curve helpers (sketch domain) ----------------------------------------------------


def _sketch_env():
    from tests.unit.sketch.fakes import sketch_nx

    nx = sketch_nx()
    return nx, nx.sketch(origin=(0, 0, 5), active=True)


def test_curve_kind_point_and_read_back_in_the_sketch_frame():
    import math

    nx, sk = _sketch_env()
    line = nx.line(sk, (0, 0, 5), (10, 0, 5))
    arc = nx.arc(sk, (20, 0, 5), 5, 0, 90)
    circle = nx.arc(sk, (40, 0, 5), 2, 0, 360)
    assert [sketch.curve_kind(nx.tx, c) for c in (line, arc, circle)] == ["line", "arc", "circle"]
    assert sketch.point(nx.tx, line, "mid") == [5.0, 0.0, 5.0]
    assert sketch.point(nx.tx, arc, "end") == pytest.approx([20, 5, 5])
    f = sketch.frame(sk)
    row = sketch.read(nx.tx, f, arc)
    assert row["center"] == [20, 0] and row["start_angle"] == pytest.approx(0)
    assert row["end_angle"] == pytest.approx(90) and row["length"] == pytest.approx(math.pi * 2.5)
    assert sketch.read(nx.tx, f, circle)["closed"] is True
    with pytest.raises(NXError) as error:
        sketch.point(nx.tx, line, "center")
    assert error.value.code == "NX_INVALID_ARGUMENT"
    with pytest.raises(NXError) as error:
        sketch.check_point(nx.tx, circle, "start", "/p")
    assert error.value.field == "/p"


def test_constraint_and_dimension_geometry_and_snake_names():
    nx, sk = _sketch_env()
    line = nx.line(sk, (0, 0, 5), (10, 0, 5))
    g = sketch.constraint_geometry(nx.tx, line, "end")
    assert (g.Geometry, g.PointType) == (line, "EndVertex")
    assert sketch.constraint_geometry(nx.tx, line).PointType == "NotSet"
    assert sketch.dimension_geometry(nx.tx, line, "mid").AssocType == "Midpoint"
    assert sketch.snake("ParallelDim") == "parallel_dim"
    c = sk.CreateHorizontalConstraint(g)
    assert sketch.constraint_type(nx.tx, c) == "horizontal"
    assert sketch.attached(nx.tx, sk, line) == [c] and sketch.constraints(nx.tx, sk) == [c]


def test_drag_solves_and_reports_the_native_status():
    nx, sk = _sketch_env()
    line = nx.line(sk, (0, 0, 5), (10, 2, 5))
    sk.CreateHorizontalConstraint(sketch.constraint_geometry(nx.tx, line))
    assert sketch.drag(nx.tx, sk, line, "end") == "Updated"
    assert line.StartPoint.Y == 2 and nx.live_builders == 0
    assert sketch.drag(nx.tx, sk, line, "end", [12, 2]) == "Updated"
    assert (line.EndPoint.X, line.EndPoint.Y) == (12, 2)


def test_set_construction_and_new_geometry():
    nx, sk = _sketch_env()
    before = sketch.tags(sk)
    line = nx.line(sk, (0, 0, 5), (10, 0, 5))
    assert sketch.new_geometry(sk, before) == [line] and sketch.geometry(sk) == [line]
    sketch.set_construction(nx.tx, [line], True)
    assert line.IsReference and nx.last("ConvertToFromReferenceBuilder").destroyed


def test_ellipse_math():
    assert sketch.ellipse_perimeter(10, 10) == pytest.approx(2 * 3.141592653589793 * 10)
    assert sketch.ellipse_perimeter(50, 5) == pytest.approx(203.198709, abs=1e-6)
    assert sketch.ellipse_point([0, 0], 20, 10, 0, 90) == pytest.approx([0, 10])
    f = {"x_axis": [1, 0, 0], "y_axis": [0, 1, 0], "normal": [0, 0, 1]}
    assert sketch.ellipse_in_frame([1, 0, 0], [0, 1, 0], [0, 0, 1], 30, 0, 90, f) == (
        pytest.approx(30),
        0,
        90,
    )
    flipped = sketch.ellipse_in_frame([-1, 0, 0], [0, 1, 0], [0, 0, -1], 0, 10, 40, f)
    assert flipped == (pytest.approx(180), -40, -10)


def test_relax_found_relations_relaxes_every_relation_nx_found():
    relaxed = []
    builder = NS(
        FindRelations=lambda: ["r1", "r2"],
        SetRelationRelaxState=lambda r, on: relaxed.append((r, on)),
    )
    assert sketch.relax_found_relations(builder) == 2
    assert relaxed == [("r1", True), ("r2", True)]
    assert sketch.relax_found_relations(NS()) == 0


def test_status_solves_a_sketch_nx_has_not_evaluated_and_reads_again(monkeypatch):
    """NX can answer NotEvaluated before it solved a sketch (right after an NX start)."""
    nx = fake_nx()
    status = NS(NotEvaluated=0, UnderConstrained=1, WellConstrained=2)
    monkeypatch.setattr(nx.nx.Sketch, "Status", status)
    s = nx.sketch()
    answers = iter([(0, 0), (1, 3)])
    s.GetStatus = lambda: next(answers)
    assert sketch.status(nx.tx, s) == {"status": "UnderConstrained", "dof": 3, "native_dof": 3}
    assert s.updates == ["Update"] and nx.undo_marks == []
    s.status, s.updates = (2, 0), []
    del s.GetStatus
    assert sketch.status(nx.tx, s)["status"] == "WellConstrained" and s.updates == []

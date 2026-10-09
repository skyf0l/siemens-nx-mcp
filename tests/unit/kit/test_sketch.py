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

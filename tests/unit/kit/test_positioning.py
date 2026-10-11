"""kit.positioning: constraint status, severity and the parts a constraint positions."""

from types import SimpleNamespace as NS

from nx_mcp.kit import positioning

STATUS = NS(Solved=9, CannotSolve=10, NotSolved=8, OutOfDate=2, Suppressed=1)
TYPE = NS(Touch=2, Concentric=7)
ALIGN = NS(CoAlign=11, ContraAlign=12)


class _Tx:
    def __init__(self):
        self.nx = NS(Positioning=NS(Constraint=NS(SolverStatus=STATUS, Type=TYPE, Alignment=ALIGN)))

    def ref(self, obj, kind, part=None):
        return {"id": f"{kind}:{obj.Tag}", "kind": kind}

    def kind_of(self, obj):
        return obj.kind


def _component(tag, prototype):
    return NS(Tag=tag, Prototype=prototype, kind="component")


def _constraint(tag, status, components, type=TYPE.Concentric, align=ALIGN.CoAlign, off=False):
    refs = [NS(GetMovableObject=lambda c=c: c) for c in components]
    return NS(Tag=tag, Suppressed=off, ConstraintType=type, ConstraintAlignment=align,
              GetConstraintStatus=lambda: getattr(STATUS, status),
              GetReferences=lambda: refs)  # fmt: skip


def test_unsolved_lists_errors_and_warnings_and_skips_solved_and_suppressed():
    pin, blk = NS(Tag=1), NS(Tag=2)
    a, b = _component(11, pin), _component(12, blk)
    cons = [
        _constraint(21, "CannotSolve", [a, b, a]),
        _constraint(22, "Solved", [a]),
        _constraint(23, "CannotSolve", [b], off=True),
        _constraint(24, "OutOfDate", [b], type=TYPE.Touch, align=ALIGN.ContraAlign),
    ]
    part = NS(ComponentAssembly=NS(Positioner=NS(Constraints=cons)))
    rows = positioning.unsolved(_Tx(), part)
    assert [(r["constraint"]["id"], r["severity"], r["status"], r["type"]) for r in rows] == [
        ("assembly_constraint:21", "error", "CannotSolve", "concentric"),
        ("assembly_constraint:24", "warning", "OutOfDate", "touch"),
    ]
    assert rows[0]["components"] == ["component:11", "component:12"]  # once each
    assert rows[0]["_constraint"] is cons[0]
    assert positioning.uses(cons[0], pin) and not positioning.uses(cons[3], pin)


def test_parts_without_a_positioner_have_no_constraints():
    assert positioning.all_constraints(NS()) == []
    assert positioning.all_constraints(NS(ComponentAssembly=NS(Positioner=None))) == []
    assert positioning.unsolved(_Tx(), NS(ComponentAssembly=None)) == []

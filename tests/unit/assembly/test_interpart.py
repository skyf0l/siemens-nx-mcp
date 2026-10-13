"""nx_create_interpart_link on the fake NX: a master part's expressions linked into
the work part through InterpartExpressionsBuilder, read back and rolled back."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.kit.objects import Expression
from tests.unit.assembly.fakes import assembly_nx

MM = NS(Name="MilliMeter", Abbreviation="mm")


class InterpartBuilder:
    """``ExpressionCollection.CreateInterpartExpressionsBuilder``: Commit adds
    ``name = "part"::source`` interpart expressions to the work part."""

    def __init__(self, nx):
        self.nx = nx
        self.pairs = []
        self.destroyed = False
        nx.interpart_builders.append(self)

    def SetExpressions(self, sources, names):
        self.pairs = list(zip(sources, names, strict=True))

    def Commit(self):
        work = self.nx.session.Parts.Work
        for src, name in self.pairs:
            e = Expression(name, f'"{src.OwningPart.Name}"::{src.Name}', src.Units)
            e.OwningPart = work
            e.IsInterpartExpression = True
            e.Value = src.Value + (1.0 if self.nx.lie else 0.0)
            e.source = src
            work.Expressions.append(e)
        return None

    def Destroy(self):
        self.destroyed = True


def _expressions(nx, part):
    part.Expressions = getattr(part, "Expressions", None) or []
    part.Expressions = type("Exprs", (list,), {})(part.Expressions)
    part.Expressions.CreateInterpartExpressionsBuilder = lambda: InterpartBuilder(nx)


@pytest.fixture
def nx():
    nx = assembly_nx()
    nx.lie = False
    nx.interpart_builders = []
    for part in (nx.blk, nx.asm):
        _expressions(nx, part)
    length = Expression("deck_len", "250", MM)
    length.Value = 250.0
    length.OwningPart = nx.blk
    nx.blk.Expressions.append(length)
    nx.length = length
    session = nx.session
    set_mark, undo = session.SetUndoMark, session.UndoToMark
    saved = {}

    def set_undo_mark(*args):
        mark = set_mark(*args)
        saved[mark] = list(nx.asm.Expressions)
        return mark

    def undo_to_mark(mark, *args):
        undo(mark, *args)
        nx.asm.Expressions[:] = saved[mark]

    session.SetUndoMark, session.UndoToMark = set_undo_mark, undo_to_mark
    return nx


def test_links_a_master_expression_into_the_work_part(nx):
    ok = run(nx, "nx_create_interpart_link", part="asm", source_part="blk",
             expressions=[{"source": "deck_len", "name": "len"}])  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    item = ok.data["items"][0]
    assert item["value"] == 250.0 and item["units"] == "mm"
    assert item["formula"] == '"blk"::deck_len'
    assert item["source"] == {"part": "blk.prt", "expression": "deck_len", "value": 250.0}
    assert [e.Name for e in nx.asm.Expressions] == ["len"]
    assert nx.interpart_builders[0].destroyed
    default = run(nx, "nx_create_interpart_link", part="asm", source_part="blk.prt",
                  expressions=[{"source": "deck_len"}])  # fmt: skip
    assert default.ok and default.data["items"][0]["expression"]["name"] == "deck_len", default


def test_refusals_change_nothing(nx):
    cases = [
        ({"source_part": "asm", "expressions": [{"source": "deck_len"}]}, "/source_part"),
        ({"source_part": "nope", "expressions": [{"source": "deck_len"}]}, "/source_part"),
        ({"source_part": "blk", "expressions": [{"source": "missing"}]}, "/expressions/0/source"),
        (
            {
                "source_part": "blk",
                "expressions": [
                    {"source": "deck_len", "name": "a"},
                    {"source": "deck_len", "name": "a"},
                ],
            },
            "/expressions/1",
        ),  # fmt: skip
    ]
    for args, field in cases:
        bad = run(nx, "nx_create_interpart_link", part="asm", **args)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    assert list(nx.asm.Expressions) == []
    run(nx, "nx_create_interpart_link", part="asm", source_part="blk",
        expressions=[{"source": "deck_len"}])  # fmt: skip
    clash = run(nx, "nx_create_interpart_link", part="asm", source_part="blk",
                expressions=[{"source": "deck_len"}])  # fmt: skip
    assert clash.error["code"] == "NX_NAME_CONFLICT", clash


def test_a_wrong_linked_value_rolls_back(nx):
    nx.lie = True
    bad = run(nx, "nx_create_interpart_link", part="asm", source_part="blk",
              expressions=[{"source": "deck_len"}])  # fmt: skip
    assert bad.error["category"] == "verification_failed", bad
    assert bad.mutation_outcome == "rolled_back" and list(nx.asm.Expressions) == []

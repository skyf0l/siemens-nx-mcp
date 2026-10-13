"""The part family table on a drawing sheet (nx_create_part_family_table) on the fake."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.kit.objects import Object
from tests.unit.drawing.fakes import drawing_nx

MEMBERS = [["pf_50", "50", "12.5"], ["pf_60", "60", "p1+1"]]


def family_nx(template=True, shown=None):
    """A family template with two members; NX's table shows ``shown`` (default the
    members, with 21 for the formula value)."""
    nx = drawing_nx()
    nx.sheet_obj = nx.make_sheet()
    cells = shown if shown is not None else [["pf_50", "50", "12.5"], ["pf_60", "60", "21"]]
    nx.uf.Part = NS(IsFamilyTemplate=lambda tag: (template,), AskFamilies=lambda tag: (1, [77]))
    nx.uf.Fam = NS(
        AskFamilyData=lambda family: NS(
            Name="fam", Attributes=["a0", "a1", "a2"], MemberCount=len(MEMBERS)
        ),  # fmt: skip
        AskAttributeData=lambda a: NS(Name={"a0": "Part_Name", "a1": "p0", "a2": "p2"}[a]),
        AskMemberRowData=lambda family, row: NS(Values=MEMBERS[row]),
    )

    def commit(b):
        table = Object("PFT")
        table.AnnotationOrigin = b.Origin.Origin.SetValue.calls[-1][0][2]
        table.OwningPart, table.IsOccurrence = nx.part, False
        nx.dependent[table.Tag] = (1, "SHT1@0")
        return table

    nx.on_commit("PartFamilyTableBuilder", commit)
    nx.part.Annotations = NS(
        PartsLists=[],
        BendTables=[],
        PartFamilyTables=NS(
            CreatePartFamilyTableBuilder=lambda t: nx.builder("PartFamilyTableBuilder", t)
        ),
    )
    nx.uf.Tabnot = NS(
        AskNmSections=lambda tag: 0,
        AskNmRows=lambda note: len(cells),
        AskNmColumns=lambda note: len(cells[0]) if cells else 0,
        AskNthRow=lambda note, i: i,
        AskNthColumn=lambda note, j: j,
        AskCellAtRowCol=lambda row, col: (row, col),
        AskEvaluatedCellText=lambda cell: cells[cell[0]][cell[1]],
    )
    return nx


def test_the_table_shows_every_member_at_its_origin():
    nx = family_nx()
    ok = run(nx, "nx_create_part_family_table", origin=[30, 250])
    assert ok.ok, ok
    assert ok.data["origin"] == pytest.approx([30, 250])
    assert ok.data["columns"] == ["Part_Name", "p0", "p2"]
    assert [row[0] for row in ok.data["rows"]] == ["pf_50", "pf_60"]
    assert ok.data["sheet"]["id"] == nx.sheet_obj.ref and nx.live_builders == 0


def test_a_part_that_is_no_family_template_is_refused_first():
    nx = family_nx(template=False)
    bad = run(nx, "nx_create_part_family_table", origin=[30, 250])
    assert bad.error["code"] == "NX_WRONG_KIND" and "nx_create_part_family" in bad.error["hint"]
    assert bad.mutation_outcome == "not_started" and not nx.builders


def test_a_table_missing_a_member_fails_the_read_back():
    nx = family_nx(shown=[["pf_50", "50", "12.5"]])
    bad = run(nx, "nx_create_part_family_table", origin=[30, 250], sheet="SHT1")
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"

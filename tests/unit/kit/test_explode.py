"""kit.explode: the explosion shown in the work view and the assembled-positions note."""

from types import SimpleNamespace as NS

from nx_mcp.kit import explode


class _Tx:
    def __init__(self, shown_tag=7, work_is_display=True, sheet=False):
        explosion = NS(Tag=7, Name="EXPLODED")
        self.view = NS(Tag=3)
        self.display = NS(
            Tag=1,
            ComponentAssembly=NS(Explosions=[NS(Tag=5, Name="OTHER"), explosion]),
            ModelingViews=NS(WorkView=self.view),
            DrawingSheets=NS(CurrentDrawingSheet=object() if sheet else None),
        )
        work = self.display if work_is_display else NS(Tag=2)
        self.session = NS(Parts=NS(Display=self.display, Work=work))
        self.uf = NS(Assem=NS(AskViewExplosion=lambda tag: shown_tag if tag == 3 else 0))


def test_shown_explosion_of_the_work_view():
    assert explode.shown(_Tx()).Name == "EXPLODED"
    assert explode.shown(_Tx(shown_tag=0)) is None
    assert explode.shown(_Tx(work_is_display=False)) is None  # a component is the work part
    assert explode.shown(_Tx(sheet=True)) is None  # a drawing sheet hides the model view


def test_shown_is_none_when_nx_refuses():
    tx = _Tx()
    tx.uf = NS()  # no UF Assem
    assert explode.shown(tx) is None
    tx = _Tx()
    tx.session.Parts.Display = None
    assert explode.shown(tx) is None


def test_assembled_note_names_the_explosion():
    note = explode.assembled_note(_Tx())
    assert "EXPLODED" in note and "assembled (unexploded)" in note
    assert explode.assembled_note(_Tx(shown_tag=0)) is None

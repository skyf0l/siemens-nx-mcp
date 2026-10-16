"""kit.selftest: NX's undo self-test on an untested build, in a scratch part only."""

from types import SimpleNamespace as NS

from nx_mcp.kit import selftest

_tags = iter(range(5000, 6000))


class Features(list):
    """A part's feature list that also holds the builder factories."""


class Part:
    def __init__(self, name):
        self.Tag = next(_tags)
        self.Name = name
        self.FullPath = name
        self.Features = Features()
        self.Bodies: list = []
        self.closed = False

    def CloseRetainUndoMarks(self, *args):
        self.closed = True


class BlockBuilder:
    def __init__(self, part, fail=False):
        self.part, self.fail = part, fail
        self.BooleanOption = NS(Type=None)
        self.destroyed = False

    def SetOriginAndLengths(self, *args):
        self.lengths = args

    def CommitFeature(self):
        if self.fail:
            raise RuntimeError("NX refused the block")
        self.part.Features.append(NS(Tag=next(_tags)))
        self.part.Bodies.append(NS(Tag=next(_tags)))

    def Destroy(self):
        self.destroyed = True


def make(*, undo_works=True, file_new_fails=False, block_fails=False):
    engineer = Part("engineer.prt")
    session_parts = NS(Display=engineer, Work=engineer, made=[])

    def file_new():
        if file_new_fails:
            raise RuntimeError("no template")
        part = Part("scratch.prt")
        session_parts.made.append(part)
        part.Features.CreateBlockFeatureBuilder = lambda null: BlockBuilder(part, block_fails)
        return NS(Commit=lambda: part, Destroy=lambda: None)

    def show(part, *options):
        session_parts.Display = session_parts.Work = part

    session_parts.FileNew = file_new
    session_parts.SetActiveDisplayRetainUndoMarks = show
    session_parts.SetWork = lambda part: setattr(session_parts, "Work", part)
    marks = {}

    def set_mark(name, visible=True):
        part = session_parts.Work
        marks[1] = (part, list(part.Features), list(part.Bodies))
        return 1

    def undo_to(mark):
        if undo_works:
            part, features, bodies = marks[mark]
            part.Features[:], part.Bodies[:] = features, bodies

    services = NS(set_mark=set_mark, undo_to=undo_to, delete_mark=lambda mark: marks.pop(mark))
    nx = NS(
        Part=NS(Units=NS(Millimeters="mm")),
        DisplayPartOption=NS(AllowAdditional=1),
        PartDisplayPartWorkPartOption=NS(SameAsDisplay=0),
        Features=NS(
            Feature=NS(Null=None),
            BlockFeatureBuilder=NS(Types=NS(OriginAndEdgeLengths="edges")),
        ),
        GeometricUtilities=NS(BooleanOperation=NS(BooleanType=NS(Create="create"))),
        Point3d=lambda *xyz: xyz,
        BasePart=NS(CloseWholeTree=NS(FalseValue=False), CloseModified=NS(CloseModified=0)),
    )
    session = NS(Parts=session_parts, ApplicationName="UG_APP_MODELING")
    tx = NS(session=session, nx=nx, services=services, workspace=NS(root="ws"))
    return tx, engineer


def test_the_self_test_passes_and_gives_the_display_back():
    tx, engineer = make()
    result = selftest.run(tx)
    assert result == {"state": "passed", "reason": None}
    [scratch] = tx.session.Parts.made
    assert scratch.closed and scratch.Features == [] and scratch.Bodies == []
    assert tx.session.Parts.Display is engineer and engineer.Features == []


def test_an_undo_that_leaves_the_block_fails_the_self_test():
    tx, engineer = make(undo_works=False)
    result = selftest.run(tx)
    assert result["state"] == "failed" and "undo left the block" in result["reason"]
    assert tx.session.Parts.made[0].closed and tx.session.Parts.Display is engineer


def test_a_block_nx_refuses_fails_and_no_scratch_part_skips():
    tx, _ = make(block_fails=True)
    failed = selftest.run(tx)
    assert failed["state"] == "failed" and "block could not be created" in failed["reason"]
    tx, engineer = make(file_new_fails=True)
    skipped = selftest.run(tx)
    assert skipped["state"] == "skipped" and "no scratch part" in skipped["reason"]
    assert tx.session.Parts.Display is engineer

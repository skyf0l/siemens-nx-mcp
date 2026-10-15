"""nx_check_drawing_changes: NX's drawing compare read from the builder, never committed."""

from types import SimpleNamespace as NS

from nx_mcp.domains.drawing.compare import counts_of, read_report
from tests.kit import run
from tests.kit.kernel import services
from tests.unit.drawing.fakes import drawing_nx

SUMMARY = [
    "Summary: 2 Changes, 1 Addition, 0 Deletions",
    "Version~2406.9160~2406.9160",
    "File Name~rev_b.prt~rev_a.prt",
    "Number of Sheets~1~1",
    "Number of Views~2~2",
    "Number of Annotations~3~2",
    "Annotations~0~1~0",
    "Dimensions~2~0~0",
]
CHANGES = ["Annotations~0~1~0", "Dimensions~2~0~0"]
DETAILS = ["Version~2406.9160~2406.9160", "Dimension~12~Value~10~12"]


def compare_nx():
    nx = drawing_nx()
    root = services(nx).root
    nx.make_sheet()
    (root / "rev_a.prt").write_bytes(b"x")
    nx.part.FullPath = str(root / "rev_b.prt")

    def create(path, reuse):
        b = nx.builder("CompareReportBuilder", path, reuse)
        b.SummaryStatusLine = SUMMARY[0]
        b.GetSummary = lambda: SUMMARY
        b.GetChangesSummary = lambda: CHANGES
        b.GetDetailSummary = lambda: DETAILS
        return b

    nx.part.DrawingCompare = NS(CreateCompareReportBuilder=create)
    return nx, root


def test_report_lines_become_sides_type_counts_and_details():
    assert counts_of(SUMMARY[0]) == {"changes": 2, "additions": 1, "deletions": 0}
    assert counts_of("Summary: 0 Changes, 0 Additions, 1 Deletion")["deletions"] == 1
    current, earlier, by_type, details = read_report(SUMMARY, CHANGES, DETAILS)
    assert current == {"file": "rev_b.prt", "sheets": 1, "views": 2, "annotations": 3}
    assert earlier["annotations"] == 2 and earlier["file"] == "rev_a.prt"
    assert by_type == [
        {"type": "Annotations", "changes": 0, "additions": 1, "deletions": 0},
        {"type": "Dimensions", "changes": 2, "additions": 0, "deletions": 0},
    ]
    assert details == ["Dimension~12~Value~10~12"]


def test_compare_reads_the_builder_and_destroys_it_uncommitted():
    nx, root = compare_nx()
    ok = run(nx, "nx_check_drawing_changes", against="rev_a.prt")
    assert ok.ok, ok
    assert ok.mutation_outcome == "not_applicable"
    assert ok.data["counts"] == {"changes": 2, "additions": 1, "deletions": 0}
    assert ok.data["against"] == "rev_a.prt" and ok.data["current"]["views"] == 2
    b = next(b for b in nx.builders if b.kind == "CompareReportBuilder")
    assert b.args[0].endswith("rev_a.prt") and b.args[1] is False
    assert b.commits == 0 and b.destroyed and nx.live_builders == 0


def test_compare_refusals():
    nx, root = compare_nx()
    missing = run(nx, "nx_check_drawing_changes", against="nope.prt")
    assert missing.error["field"] == "/against" and missing.error["code"] == "NX_FILE_NOT_FOUND"
    (root / "rev_b.prt").write_bytes(b"x")
    itself = run(nx, "nx_check_drawing_changes", against="rev_b.prt")
    assert itself.error["code"] == "NX_INVALID_ARGUMENT" and itself.error["field"] == "/against"
    nx.part.DrawingSheets.clear()
    bare = run(nx, "nx_check_drawing_changes", against="rev_a.prt")
    assert bare.error["code"] == "NX_WRONG_APPLICATION"


class _NativeError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.ErrorCode = code


def test_an_earlier_revision_without_sheets_is_a_wrong_against():
    nx, root = compare_nx()

    def refuse(code):
        def create(path, reuse):
            raise _NativeError("There are no drawing sheets in the selected part.", code)

        return create

    nx.part.DrawingCompare = NS(CreateCompareReportBuilder=refuse(735029))
    bare = run(nx, "nx_check_drawing_changes", against="rev_a.prt")
    assert bare.error["code"] == "NX_INVALID_ARGUMENT" and bare.error["field"] == "/against"
    assert bare.error["retry"] == "after_fix" and bare.error["nx_code"] == 735029
    assert "rolled back" not in bare.error["hint"]
    assert bare.mutation_outcome == "not_applicable"
    nx.part.DrawingCompare = NS(CreateCompareReportBuilder=refuse(123456))
    other = run(nx, "nx_check_drawing_changes", against="rev_a.prt")
    assert other.error["code"] == "NX_EXPORT_FAILED" and "rolled back" not in other.error["hint"]

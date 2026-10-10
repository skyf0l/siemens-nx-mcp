"""nx_export_expressions and nx_import_expressions on the shared fake.

The fake import mirrors NX 2406: Replace accepts a changed formula naming a missing
expression (evaluating it raises 1050007) but raises 1050053 for a new one;
DontReplace raises 1050053 on any existing name; DeleteImports deletes the listed
expressions nothing references.
"""

from pathlib import Path
from types import SimpleNamespace as NS

from nx_mcp.domains.modeling import expression_files as ef
from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import NXException
from tests.unit.modeling.fakes import modeling_nx

BOM = b"\xef\xbb\xbf"
EXP = BOM + b"// Version:  3\r\n[MilliMeter]len=10\r\n[MilliMeter]wall=len/4\r\nratio=3\r\n"
MM = NS(Name="MilliMeter", Abbreviation="mm")


def _broken(*_):
    raise NXException(1050007, "The specified expression variable does not exist.")


def _formula(data, name):
    for line in data.decode("utf-8-sig").splitlines():
        left, _, right = line.partition("=")
        if right and left.split("]")[-1].strip() == name:
            return right.strip()
    raise AssertionError(name)


def exp_nx():
    nx = modeling_nx()
    col = nx.part.Expressions
    col.ExportMode = NS(WorkPart="work")
    col.SortType = NS(AlphaNum="alpha", TimeStamp="time")
    col.ImportMode = NS(Replace="replace", DontReplace="keep", DeleteImports="delete")
    nx.exports, nx.imports = [], []

    def export(mode, name, sort):
        nx.exports.append((mode, sort))
        with open(name, "xb") as out:
            out.write(EXP)

    def import_file(path, mode):
        data = Path(path).read_bytes()
        nx.imports.append((mode, data))
        for name, unit in ef.file_entries(data):
            formula = _formula(data, name)
            old = next((e for e in col if e.Name == name), None)
            if mode == "delete":
                if old is not None and not old.dependents:
                    col.remove(old)
            elif old is None:
                if "missing" in formula:
                    raise NXException(1050053, "Expression import failed.")
                e = nx.expression(name, formula, float(formula) if formula.isdigit() else 0.0)
                e.Units = MM if unit == "MilliMeter" else None
            elif mode == "keep":
                raise NXException(1050053, "Expression import failed.")
            else:
                old.RightHandSide = formula
                if "missing" in formula:
                    old.GetValueUsingUnits = _broken
        return True, []

    col.ExportToFile = export
    col.ImportFromFile = import_file
    return nx


def write(nx, name, data):
    path = services(nx).root / name
    path.write_bytes(data)
    return str(path)


def test_line_parser_reads_bom_types_units_and_names():
    data = BOM + b'title=1\r\n(String) s="a"\r\n[Degrees]ang=5\r\n  x_1 = 2 // n\r\nbad line\r\n'
    assert ef.file_entries(data) == [("title", None), ("s", None), ("ang", "Degrees"),
                                     ("x_1", None)]  # fmt: skip
    kept, names = ef.keep_lines(data, ["a*", "x?1"])
    assert names == ["ang", "x_1"] and b"bad line" in kept and b"title" not in kept
    _, names = ef.keep_lines(data, [], keep=lambda n: n != "ang")
    assert names == ["title", "s", "x_1"]


def test_export_all_and_filtered_never_overwrites():
    nx = exp_nx()
    nx.expression("len", "10")
    root = services(nx).root
    ok = run(nx, "nx_export_expressions", path=str(root / "out" / "all.exp"))
    assert ok.ok, ok
    assert ok.data["path"] == "out/all.exp" and ok.data["count"] == 3
    assert ok.data["expressions"] == [
        {"name": "len", "formula": "10", "units": "mm"},
        {"name": "wall", "formula": "len/4", "units": "mm"},
        {"name": "ratio", "formula": "3", "units": "unitless"},
    ]
    assert nx.exports == [("work", "alpha")] and not ok.data["overwritten"]
    assert (root / "out" / "all.exp").read_bytes() == EXP
    part = run(nx, "nx_export_expressions", path=str(root / "out" / "some.exp"),
               names=["w*", "ratio"], sort="timestamp")  # fmt: skip
    assert [r["name"] for r in part.data["expressions"]] == ["wall", "ratio"]
    assert nx.exports[-1] == ("work", "time") and part.data["count"] == 2
    assert b"len=10" not in (root / "out" / "some.exp").read_bytes()
    again = run(nx, "nx_export_expressions", path=str(root / "out" / "all.exp"))
    assert again.error["code"] == "NX_ALREADY_EXISTS" and "overwrite" in again.error["message"]
    over = run(nx, "nx_export_expressions", path=str(root / "out" / "some.exp"), overwrite=True)
    assert over.ok and over.data["overwritten"] and over.data["count"] == 3, over
    assert (root / "out" / "some.exp").read_bytes() == EXP
    missing = run(nx, "nx_export_expressions", path=str(root / "out" / "x.exp"), names=["zz*"])
    assert missing.error["code"] == "NX_NOT_FOUND" and missing.error["field"] == "/names"
    assert missing.error["details"]["available"] == ["len", "wall", "ratio"]
    assert not (root / "out" / "x.exp").exists()
    bad = run(nx, "nx_export_expressions", path=str(root / "out" / "x.txt"))
    assert bad.error["field"] == "/path"


def test_import_replace_reports_created_and_changed():
    nx = exp_nx()
    nx.expression("len", "20", 20.0).Units = MM
    ok = run(nx, "nx_import_expressions", path=write(nx, "in.exp", EXP))
    assert ok.ok, ok
    assert ok.data["file_expressions"] == 3 and ok.mutation_outcome == "committed"
    assert [r["formula"] for r in ok.data["created"]] == ["len/4", "3"]
    changed = ok.data["changed"]
    assert changed[0]["previous"] == "20" and changed[0]["formula"] == "10"
    assert ok.data["path"] == "in.exp" and ok.data["skipped"] == ok.data["deleted"] == []


def test_import_keep_imports_only_new_names():
    nx = exp_nx()
    nx.expression("len", "20", 20.0).Units = MM
    ok = run(nx, "nx_import_expressions", path=write(nx, "in.exp", EXP), mode="keep")
    assert ok.ok, ok
    mode, sent = nx.imports[-1]
    assert mode == "keep" and b"len=" not in sent and b"wall=" in sent
    assert ok.data["skipped"] == ["len"] and ok.data["changed"] == []
    assert [r["formula"] for r in ok.data["created"]] == ["len/4", "3"]
    again = run(nx, "nx_import_expressions", path=write(nx, "in2.exp", EXP), mode="keep")
    assert again.ok and again.data["skipped"] == ["len", "wall", "ratio"] and len(nx.imports) == 1


def test_import_delete_removes_unreferenced_names():
    nx = exp_nx()
    used = nx.expression("len", "20", 20.0)
    used.dependents = [nx.expression("p1", "len")]
    nx.expression("ratio", "3", 3.0)
    ok = run(nx, "nx_import_expressions", path=write(nx, "d.exp", b"len=1\r\nratio=4\r\n"),
             mode="delete")  # fmt: skip
    assert ok.ok, ok
    assert ok.data["deleted"] == ["ratio"] and ok.data["skipped"] == ["len"]
    assert ok.data["changed"] == [] and nx.imports[-1][0] == "delete"


def test_file_rows_read_formulas_units_and_strip_comments():
    data = BOM + b'[MilliMeter]a=b*2 // note\r\n(String) s="x//y"\r\n[Degrees]ang=5\r\n'
    assert ef.file_rows(data) == [
        {"name": "a", "formula": "b*2", "units": "mm"},
        {"name": "s", "formula": '"x//y"', "units": "unitless"},
        {"name": "ang", "formula": "5", "units": "deg"},
    ]
    rows = [{"name": "n", "formula": 'if (a > 1) (sin(b)) else (1e5*c) + "q"'},
            {"name": "m", "formula": "a+1"}]  # fmt: skip
    assert ef.unknown_names(rows, {"a"}) == [{"name": "n", "uses": ["b", "c"]}]


def test_import_failure_names_the_unknown_expression():
    nx = exp_nx()
    nx.expression("len", "10", 10.0)
    out = run(nx, "nx_import_expressions",
              path=write(nx, "n.exp", b"ok=len*2\r\nfresh=missing+len\r\n"))  # fmt: skip
    assert out.error["code"] == "NX_IMPORT_FAILED" and out.error["nx_code"] == 1050053
    assert "fresh uses missing" in out.error["message"]
    assert out.error["details"]["unknown_names"] == [{"name": "fresh", "uses": ["missing"]}]


def test_import_delete_skips_names_the_part_lacks():
    nx = exp_nx()
    nx.expression("ratio", "3", 3.0)
    real = nx.part.Expressions.ImportFromFile

    def strict(path, mode):  # NX 2406: DeleteImports fails on a name the part lacks
        names = ef.file_names(Path(path).read_bytes())
        if mode == "delete" and any(n not in {e.Name for e in nx.part.Expressions} for n in names):
            raise NXException(1050053, "Expression import failed.")
        return real(path, mode)

    nx.part.Expressions.ImportFromFile = strict
    ok = run(nx, "nx_import_expressions", path=write(nx, "d.exp", b"ratio=4\r\nextra=7\r\n"),
             mode="delete")  # fmt: skip
    assert ok.ok, ok
    assert ok.data["deleted"] == ["ratio"] and ok.data["skipped"] == ["extra"]
    assert b"extra" not in nx.imports[-1][1]
    none = run(nx, "nx_import_expressions", path=write(nx, "e.exp", b"extra=7\r\n"),
               mode="delete")  # fmt: skip
    assert none.ok and none.data["skipped"] == ["extra"] and none.data["deleted"] == []


def test_import_errors_roll_back_before_the_update():
    nx = exp_nx()
    wall = nx.expression("wall", "2", 2.0)
    length = nx.expression("len", "10", 10.0)
    length.Units = MM
    out = run(nx, "nx_import_expressions", path=write(nx, "m.exp", b"wall=missing_one*2\r\n"))
    assert out.error["code"] == "NX_IMPORT_FAILED" and out.mutation_outcome == "rolled_back"
    assert out.error["details"]["expressions"][0]["nx_code"] == 1050007
    new = run(nx, "nx_import_expressions", path=write(nx, "n.exp", b"fresh=missing+1\r\n"))
    assert new.error["code"] == "NX_IMPORT_FAILED" and new.error["nx_code"] == 1050053
    assert new.error["field"] == "/path" and new.mutation_outcome == "rolled_back"
    del wall.GetValueUsingUnits
    wall.dependents, length.dependents = [length], [wall]
    cyc = run(nx, "nx_import_expressions", path=write(nx, "c.exp", b"wall=len*2\r\n"))
    assert cyc.error["details"]["expressions"][0]["problem"] == "depends on itself"
    assert cyc.mutation_outcome == "rolled_back"
    wall.dependents = length.dependents = []
    units = run(nx, "nx_import_expressions", path=write(nx, "u.exp", b"[Degrees]len=12\r\n"))
    assert units.error["code"] == "NX_INVALID_ARGUMENT" and units.mutation_outcome == "not_started"
    assert units.error["details"]["expressions"][0] == {
        "name": "len", "file_units": "Degrees", "units": "mm"}  # fmt: skip
    empty = run(nx, "nx_import_expressions", path=write(nx, "e.exp", b"// nothing\r\n"))
    assert empty.error["field"] == "/path" and empty.mutation_outcome == "not_started"
    big = run(nx, "nx_import_expressions",
              path=write(nx, "big.exp", b"a=1\n" + b" " * ef.MAX_FILE))  # fmt: skip
    assert big.error["field"] == "/path" and big.mutation_outcome == "not_started"
    nx.update_errors = 2
    upd = run(nx, "nx_import_expressions", path=write(nx, "g.exp", b"[MilliMeter]len=12\r\n"))
    assert upd.error["code"] == "NX_UPDATE_FAILED" and upd.mutation_outcome == "rolled_back"
    nx.update_errors = 0
    missing = run(nx, "nx_import_expressions", path=str(services(nx).root / "nope.exp"))
    assert missing.error["field"] == "/path"

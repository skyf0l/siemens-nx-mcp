"""Errors that say what to change: known NX codes, retry classes, close part names,
the argument an error is about, the target file instead of "the work part", formats in
words and quantity hints."""

from __future__ import annotations

from typing import Annotated

import pytest

from nx_mcp.core import errors
from nx_mcp.core import types as T
from nx_mcp.core.catalogue import Param
from nx_mcp.core.errors import NXError, classify, nearest, pinpoint
from nx_mcp.core.validate import pattern_text, validate
from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Part
from tests.kit.parts import with_parts
from tests.unit.modeling.fakes import modeling_nx


class NativeError(Exception):
    def __init__(self, code, message="native"):
        super().__init__(message)
        self.ErrorCode = code


# -- known NX codes and retry classes --------------------------------------------


@pytest.mark.parametrize(
    ("nx_code", "code", "words"),
    [
        (671729, "NX_INVALID_ARGUMENT", "at least 3 points"),
        (650008, "NX_INVALID_ARGUMENT", "no mirror"),
        (3545001, "NX_BUILD_FAILED", "does not intersect"),
        (670416, "NX_BUILD_FAILED", "misses the faces"),
        (1395078, "NX_BUILD_FAILED", "yields no curve"),
        (4120001, "NX_WRONG_GEOMETRY", "revolve"),
        (4685108, "NX_UNSUPPORTED_OPTION", "up to date"),
    ],
)
def test_known_nx_codes_say_what_to_change(nx_code, code, words):
    error = classify(NativeError(nx_code, "NX text"), default="NX_BUILD_FAILED")
    assert error.code == code and error.nx_code == nx_code and words in error.hint
    assert error.message == "NX text"  # NX's own reason stays the message


def test_a_tools_own_default_keeps_its_code_and_gains_the_known_hint():
    error = classify(NativeError(640005, "Invalid file name"), default="NX_SAVE_FAILED")
    assert error.code == "NX_SAVE_FAILED" and "letters, digits" in error.hint
    unknown = classify(NativeError(12345, "x"), default="NX_SAVE_FAILED")
    assert unknown.hint is None  # the category's hint then


def test_a_verification_failure_is_fixed_by_a_changed_call():
    error = NXError("NX_VERIFICATION_FAILED", "volume differs")
    assert error.retry == "after_fix"
    assert "change that input" in error.error_dict()["hint"]
    assert classify(ValueError("bug")).retry == "ask_engineer"  # our bug stays a report


# -- close part names -------------------------------------------------------------


def test_nearest_lists_close_names_the_callers_first_and_never_everything():
    loaded = [f"c{i:02d}asm.prt" for i in range(12)] + ["housing.prt", "housing_v2.prt"]
    assert nearest("housng", loaded)[:2] == ["housing.prt", "housing_v2.prt"]
    assert len(nearest("c05asm", loaded)) == 3
    assert nearest("zzz", loaded) == []  # nothing like it: no unrelated names
    assert nearest("zzz", loaded, first=["housing.prt"]) == ["housing.prt"]
    tie = nearest("plate", ["plate.prt", "plates.prt", "plated.prt"], first=["plated.prt"])
    assert tie[0] == "plate.prt" and tie.index("plated.prt") < tie.index("plates.prt")
    text = errors.listed_names("housng", loaded)
    assert text.startswith("housing.prt, housing_v2.prt") and "(14 loaded" in text
    assert errors.listed_names("x", []) == "none is loaded"


def test_an_unknown_part_lists_the_closest_three_and_the_count():
    nx = modeling_nx()
    root = services(nx).root
    with_parts(nx, root)
    nx.part.FullPath = str(root / "work.prt")
    parts = nx.session.Parts
    work, display = parts.Work, parts.Display
    for i in range(8):
        Part(nx.session, root / f"other/c{i}asm.prt")
    Part(nx.session, root / "smoke/plate.prt")
    parts.Work, parts.Display = work, display
    r = run(nx, "nx_create_primitive", part="plat", length=1, width=1, height=1)
    assert r.error["code"] == "NX_NOT_FOUND" and r.error["field"] == "/part", r
    hint = r.error["hint"]
    assert hint.startswith("Use a loaded file name: plate.prt") and "10 loaded" in hint, hint
    assert "c7asm.prt" not in hint
    rows = r.error["details"]["loaded_parts"]
    assert rows[0]["file"] == "plate.prt" and len(rows) <= 3
    assert r.error["details"]["loaded_count"] == 10


# -- the argument an error is about -----------------------------------------------


def test_pinpoint_names_the_argument_holding_the_ref():
    error = NXError("NX_WRONG_KIND", "b:3 is a body; expected face", details={"ref": "b:3"})
    pinpoint(error, {"first": "b:3", "second": "fc:9"})
    assert error.field == "/first"
    step = NXError("NX_STALE_REFERENCE", "s:4 is gone", details={"ref": "s:4"})
    args = {
        "operations": [
            {"tool": "a", "arguments": {}},
            {"tool": "b", "arguments": {"sketch": "s:4"}},
        ]
    }
    assert pinpoint(step, args).field == "/operations/1/arguments/sketch"
    twice = NXError("NX_STALE_REFERENCE", "x", details={"ref": "e:1"})
    assert pinpoint(twice, {"a": "e:1", "b": ["e:1"]}).field is None  # ambiguous: left
    kept = NXError("NX_WRONG_KIND", "x", field="/edges/2", details={"ref": "e:1"})
    assert pinpoint(kept, {"edges": ["e:9", "e:8", "e:1"]}).field == "/edges/2"


def test_pinpoint_points_a_scalar_argument_without_an_index():
    error = NXError("NX_WRONG_KIND", "/face/0: e:3 is an edge", field="/face/0")
    pinpoint(error, {"face": "e:3"})
    assert error.field == "/face" and error.message.startswith("/face: ")
    listed = NXError("NX_WRONG_KIND", "/faces/5: x", field="/faces/5")
    assert pinpoint(listed, {"faces": ["fc:1"]}).field == "/faces/5"  # a list: unchanged


def test_pinpoint_names_the_target_file_instead_of_the_work_part():
    error = NXError(
        "NX_NOT_FOUND", "The work part has no expression 'zzz'", hint="Open the work part"
    )
    pinpoint(error, {}, "spring.prt")
    assert error.message == "spring.prt has no expression 'zzz'"
    assert error.hint == "Open the work part"  # hints keep their wording
    other = NXError("NX_INVALID_ARGUMENT", "make it the work part first")
    assert pinpoint(other, {}, "a.prt").message == "make it the work part first"


# -- formats and quantities -------------------------------------------------------


def test_a_pattern_error_says_the_format_in_words_not_the_field_description():
    name = Annotated[str, T.Text(64, r"^[A-Za-z0-9_][A-Za-z0-9_ .\-]{0,63}$"), T.Doc("Rename it")]
    with pytest.raises(NXError) as caught:
        validate({"new_name": Param("new_name", name, None, True)}, {"new_name": "bad/name!"})
    message = caught.value.message
    assert "Rename it" not in message and "letters, digits, '_', space, '.' and '-'" in message
    assert "(at most 64 characters)" in message and "'bad/name!'" in message
    assert pattern_text(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$") == (
        "letters, digits and '_', starting with a letter or '_'"
    )
    assert pattern_text(r"^[^=;]+$") == "any text without '=' and ';'"
    assert pattern_text(r"^\S(.*\S)?$") == "text without leading or trailing spaces"
    assert pattern_text(r"^(last|latest)$") is None and pattern_text(r"^nx_[a-z]+$") is None


def test_a_unit_of_another_dimension_is_named():
    with pytest.raises(ValueError, match="'30 deg' is an angle"):
        T.quantity_formula("30 deg", "length")
    with pytest.raises(ValueError, match="'10 mm' is a length"):
        T.quantity_formula("10 mm", "angle")
    with pytest.raises(ValueError, match="takes no NX expression names or formulas"):
        T.convert_quantity("abc", "angle", "deg")


def test_a_surface_analysis_is_a_ref_kind_of_its_own(tmp_path):
    from nx_mcp.core.references import PREFIX
    from tests.core.fakes import FakeServices

    assert "analysis" in T.KINDS and PREFIX["analysis"] == "an"
    assert list(PREFIX.values()).count("an") == 1
    assert "facet_body" in T.KINDS and PREFIX["facet_body"] == "fb"
    assert list(PREFIX.values()).count("fb") == 1
    svc = FakeServices(tmp_path)
    analysis = svc.part.add("analysis", "Reflection Analysis 1")
    row = svc.references.row(analysis, "analysis")
    assert row["id"].startswith("an:") and row["kind"] == "analysis"
    assert svc.references.resolve(row["id"], ("analysis",)) is analysis
    with pytest.raises(NXError) as wrong:
        svc.references.resolve(row["id"], ("body",))
    assert wrong.value.code == "NX_WRONG_KIND"


def test_off_sheet_is_a_warning_code():
    assert "NX_OFF_SHEET" in errors.WARNING_CODES and "NX_OFF_SHEET" not in errors.CODES


def test_an_unloaded_part_file_on_disk_is_pointed_at_nx_open_part():
    nx = modeling_nx()
    root = services(nx).root
    with_parts(nx, root)
    nx.part.FullPath = str(root / "work.prt")
    (root / "smoke").mkdir(exist_ok=True)
    (root / "smoke" / "ghost.prt").write_bytes(b"x")
    r = run(nx, "nx_create_primitive", part="smoke/ghost.prt", length=1, width=1, height=1)
    assert r.error["code"] == "NX_NOT_FOUND" and "nx_open_part" in r.error["hint"], r
    gone = run(nx, "nx_create_primitive", part="nowhere", length=1, width=1, height=1)
    assert "nx_open_part" in gone.error["hint"] and "Use a loaded file name" in gone.error["hint"]

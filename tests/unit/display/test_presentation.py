"""nx_save_presentation and nx_restore_display(presentation=...)."""

import json

import pytest

from nx_mcp.kit import geom
from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import point
from tests.unit.display.fakes import display_nx, nx_matrix

ROTATION = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]


@pytest.fixture
def saved():
    nx = display_nx()
    body = nx.block(1, 1, 1)
    body.faces[0].Color = 33
    run(nx, "nx_set_camera", rotation=ROTATION, origin=[2, 3, 4], scale=2)
    run(nx, "nx_create_section_view", plane={"origin": [0, 0, 0.5], "normal": [0, 0, 1]})
    path = services(nx).root / "views" / "review.json"
    ok = run(nx, "nx_save_presentation", path=str(path))
    return nx, body, path, ok


def test_save_writes_camera_section_and_appearance(saved):
    nx, body, path, ok = saved
    assert ok.ok and ok.data["path"] == "views/review.json" and ok.data["sections"] == 1
    assert ok.data["objects"] == 2  # the body and the one face coloured unlike it
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format"] == "nx-mcp-presentation" and data["version"] == 2
    assert data["camera"]["origin"] == [2, 3, 4] and data["camera"]["scale"] == 2
    assert data["section"] == {"origin": [0, 0, 0.5], "normal": [0, 0, 1], "cap": True}
    face = next(
        r for r in data["display"] if r["locator"]["journal_id"] == body.faces[0].JournalIdentifier
    )
    assert face["color_index"] == 33 and face["locator"]["kind"] == "face"
    again = run(nx, "nx_save_presentation", path=str(path))
    assert again.error["code"] == "NX_ALREADY_EXISTS" and again.mutation_outcome == "not_started"


def test_restore_puts_camera_section_and_colours_back(saved):
    nx, body, path, _ = saved
    body.faces[0].Color = 5
    body.IsBlanked = True
    view = nx.work_view
    run(nx, "nx_set_camera", orientation="top", fit=True)
    run(nx, "nx_edit_section_view", section=nx.ref_of(nx.part.DynamicSections[0], "section_view"),
        plane={"origin": [0, 0, 0.2], "normal": [1, 0, 0]}, enabled=False)  # fmt: skip
    ok = run(nx, "nx_restore_display", presentation=str(path))
    assert ok.ok and ok.data["identical"] is True and ok.data["count"] == 2
    assert body.faces[0].Color == 33 and not body.IsBlanked
    assert ok.data["camera"]["origin"] == [2, 3, 4] and ok.data["camera"]["scale"] == 2
    section = nx.part.DynamicSections[0]
    assert section.origin == [0, 0, 0.5] and section.normal == [0, 0, 1]
    assert view.DisplaySectioningToggle and view.ActiveDynamicSection is section


def _live_camera(nx, monkeypatch):
    """Services whose kept camera is the work view's (the shared fake keeps its own)."""
    kit, view = services(nx), nx.work_view
    restore = kit.restore_display

    def kept():
        return {"rotation": geom.rows(view.Matrix), "origin": list(geom.xyz(view.Origin)),
                "scale": view.Scale}  # fmt: skip

    def put_back(camera, records, highlights):
        restore(camera, records, highlights)
        if camera is not None:
            view.SetRotationTranslationScale(
                nx_matrix(camera["rotation"]), point(*camera["origin"]), camera["scale"]
            )

    monkeypatch.setattr(kit, "display_camera", kept)
    monkeypatch.setattr(kit, "restore_display", put_back)


def test_a_presentation_restore_can_be_undone(saved, monkeypatch):
    nx, body, path, _ = saved
    _live_camera(nx, monkeypatch)
    body.faces[0].Color = 5
    run(nx, "nx_set_camera", orientation="top")
    before = run(nx, "nx_get_camera").data
    ok = run(nx, "nx_restore_display", presentation=str(path))
    assert ok.ok and ok.restore_id and body.faces[0].Color == 33
    undo = run(nx, "nx_restore_display", restore_id="last")
    assert undo.ok and undo.data["restored"] == ok.restore_id and body.faces[0].Color == 5
    assert undo.data["camera"]["view"] == "top" == before["view"]
    assert "restore_id" not in undo.envelope


@pytest.mark.parametrize("fault", ["owner", "format", "missing", "camera"])
def test_a_bad_presentation_changes_nothing(saved, fault):
    nx, body, path, _ = saved
    data = json.loads(path.read_text(encoding="utf-8"))
    if fault == "owner":
        data["part_path"] = "elsewhere/other.prt"
    if fault == "format":
        data["version"] = 1
    if fault == "missing":
        data["display"][-1]["locator"]["journal_id"] = "GONE(1)"
    if fault == "camera":
        data["camera"]["scale"] = 0
    broken = path.with_name("broken.json")
    broken.write_text(json.dumps(data), encoding="utf-8")
    body.Color = 13
    bad = run(nx, "nx_restore_display", presentation=str(broken))
    assert not bad.ok and bad.mutation_outcome == "not_started"
    assert bad.error["field"] == "/presentation" and body.Color == 13
    expected = {
        "owner": "wrong_owner", "format": "unsupported", "missing": "stale_reference",
        "camera": "invalid_argument",
    }  # fmt: skip
    assert bad.error["category"] == expected[fault]


def test_not_json_is_refused(saved):
    nx, _, path, _ = saved
    broken = path.with_name("text.json")
    broken.write_text("{not json", encoding="utf-8")
    bad = run(nx, "nx_restore_display", presentation=str(broken))
    assert bad.error["code"] == "NX_UNSUPPORTED_FILE_TYPE"


def test_save_stores_a_workspace_path_and_overwrites_on_request(saved):
    nx, _body, path, ok = saved
    nx.part.FullPath = str(services(nx).root / "parts" / "work.prt")
    again = run(nx, "nx_save_presentation", path=str(path), overwrite=True)
    assert again.ok and again.data["path"] == "views/review.json", again.error
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["part_path"] == "parts/work.prt"
    applied = run(nx, "nx_restore_display", presentation=str(path))
    assert applied.ok, applied.error


def test_overwrite_refuses_a_file_that_is_not_a_presentation(saved):
    nx, _body, path, _ok = saved
    other = path.with_name("notes.json")
    other.write_text("{}", encoding="utf-8")
    bad = run(nx, "nx_save_presentation", path=str(other), overwrite=True)
    assert bad.error["code"] == "NX_UNSUPPORTED_FILE_TYPE" and bad.mutation_outcome == "not_started"


def test_skip_missing_applies_what_is_left(saved):
    nx, body, path, _ = saved
    data = json.loads(path.read_text(encoding="utf-8"))
    data["display"][0]["locator"]["journal_id"] = "GONE(9)"
    path.write_text(json.dumps(data), encoding="utf-8")
    strict = run(nx, "nx_restore_display", presentation=str(path))
    assert strict.error["code"] == "NX_STALE_REFERENCE" and "skip_missing" in strict.error["hint"]
    body.faces[0].Color = 5
    loose = run(nx, "nx_restore_display", presentation=str(path), skip_missing=True)
    assert loose.ok, loose.error
    assert loose.data["count"] == len(data["display"]) - 1
    assert any("GONE(9)" in w["message"] for w in loose.warnings)
    alone = run(nx, "nx_restore_display", restore_id="last", skip_missing=True)
    assert alone.error["field"] == "/skip_missing"


def test_a_part_of_the_same_file_name_takes_the_presentation(saved):
    nx, _body, path, _ = saved
    data = json.loads(path.read_text(encoding="utf-8"))
    name = data["part_path"].rsplit("/", 1)[-1]
    data["part_path"] = "elsewhere/" + name
    path.write_text(json.dumps(data), encoding="utf-8")
    ok = run(nx, "nx_restore_display", presentation=str(path))
    assert ok.ok, ok.error
    data["part_path"] = "elsewhere/other.prt"
    path.write_text(json.dumps(data), encoding="utf-8")
    bad = run(nx, "nx_restore_display", presentation=str(path))
    assert bad.error["code"] == "NX_WRONG_PART_CONTEXT"

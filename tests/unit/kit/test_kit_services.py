"""The shared fake kernel seam (tests/kit) supports sheet tools and view rollback."""

import pytest

from nx_mcp.core import catalogue
from tests.core import tools
from tests.kit import fake_nx, run

SPECS = tools.specs()


@pytest.fixture
def registered():
    names = ("nx_create_label", "nx_set_look_kit")
    catalogue.register(SPECS["nx_create_label"])
    look = catalogue.make_spec(
        nx_set_look_kit, effect="view", label="Look", keywords=tools.KEYWORDS,
        domain="display", restorable=True, example={"body": "b:1"},
    )  # fmt: skip
    catalogue.register(look)
    yield
    for name in names:
        catalogue.unregister(name)


def nx_set_look_kit(tx, *, body: tools.RefTo("body"), fail: bool = False) -> tools.Looked:  # type: ignore[valid-type]
    """Hide a body and zoom."""
    obj = tx.resolve(body, "body")
    tx.display.keep([obj], appearance=True)
    tx.display.keep_camera()
    tx.session  # noqa: B018
    obj.Blank()
    obj.Color = 3
    tx.services.fake.camera["scale"] = 4.0
    if fail:
        tools.fail("NX_BUILD_FAILED", "no")
    return {"count": 1}


def test_sheet_tool_gets_the_views_sheet_units(registered):
    nx = fake_nx("mm")
    inch = nx.sheet("SHT_IN", units="in")
    view = nx.view(inch)
    ok = run(nx, "nx_create_label", origin=[1, 2], height="12.7 mm", view=nx.ref_of(view))
    assert ok.ok, ok
    assert ok.data["height"] == pytest.approx(0.5) and ok.data["sheet"] == "SHT_IN"
    assert ok.units == {"length": "mm", "angle": "deg", "sheet_length": "in"}


def test_view_tool_restores_on_failure_and_returns_restore_id(registered):
    nx = fake_nx()
    body = nx.block(1, 1, 1)
    bad = run(nx, "nx_set_look_kit", body=body.ref, fail=True)
    assert bad.mutation_outcome == "rolled_back"
    assert not body.IsBlanked and body.Color == 7 and nx.camera["scale"] == 1.0
    ok = run(nx, "nx_set_look_kit", body=body.ref)
    assert ok.restore_id in nx.display_snapshots and body.IsBlanked

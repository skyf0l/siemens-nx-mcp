"""The side-effect observer behind ``not_started``: a failure is ``not_started``
only when nothing NX, file or process related ran before it (NXServices.observe)."""

import contextlib
import sys

from nx_mcp import side_effects
from nx_mcp.core.errors import NXError
from tests.kit.objects import Body


class NXList(list):
    """A C-implemented method owned by an NXOpen-module type, like NXOpen calls."""


NXList.__module__ = "NXOpen.Fake"


def _touched(rig, step):
    with rig.e.observe() as observer, contextlib.suppress(Exception):
        step()
    assert sys.getprofile() is None
    return observer.touched


def _fail(*_):
    raise NXError("NX_INVALID_ARGUMENT", "bad")


def test_argument_errors_before_any_side_effect_leave_the_observer_untouched(rig):
    assert not _touched(rig, lambda: _fail())
    assert not _touched(rig, lambda: float("not a number"))
    # A lookup that fails hands out no NX object.
    assert not _touched(rig, lambda: rig.e.objects.resolve("obj_missing"))


def test_possible_side_effects_touch_the_observer(rig, tmp_path):
    body = rig.ref(Body())
    assert _touched(rig, lambda: _fail(rig.e.objects.resolve(body)))
    assert _touched(rig, lambda: _fail(NXList().append(1)))
    assert _touched(rig, lambda: _fail((tmp_path / "f.txt").write_text("x")))
    assert not _touched(rig, lambda: _fail((tmp_path / "f.txt").read_text()))


def test_nested_observers_mark_the_outer_call_touched():
    outer = side_effects.SideEffectObserver().start()
    inner = side_effects.SideEffectObserver().start()
    inner.stop()
    assert outer.touched
    outer.stop()
    assert sys.getprofile() is None

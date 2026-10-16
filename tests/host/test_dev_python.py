"""Development snippets (``nx_dev_python``): the no-work-part guard and the time limit."""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp import bridge, interactive
from nx_mcp.core.errors import NXError
from nx_mcp.host import dispatch, watchdog
from tests.host.test_quiet import services


def _snippet_host(s):
    host = NS(nx=s.nx, session=s.session, services=s)
    s.dev_tools["nx_dev_python"] = lambda code, timeout=None, allow_no_part=False: (
        interactive.InteractiveHost._dev_python(host, code, timeout, allow_no_part)
    )
    return host


# -- the watchdog ------------------------------------------------------------------------


def test_the_deadline_stops_a_python_loop_and_removes_its_trace():
    before = sys.gettrace()
    now = [0.0]

    def loop():
        while True:
            now[0] += 1.0  # the clock moves with each line

    with (
        pytest.raises(watchdog.ProbeTimeout) as stopped,
        watchdog.deadline(5, clock=lambda: now[0]),
    ):
        loop()
    assert stopped.value.seconds == 5 and " in loop" in stopped.value.where
    assert "loop" in stopped.value.stack
    assert sys.gettrace() is before  # normal tools never run traced


def test_the_backstop_stops_a_one_line_spin_and_leaves_nothing_pending():
    # Python 3.10 sends the trace hook no events for this loop: the timer thread stops it.
    started = time.monotonic()
    with pytest.raises(watchdog.ProbeTimeout) as stopped, watchdog.deadline(0.2):
        while True:
            pass  # noqa: E701
    assert time.monotonic() - started < 0.2 + watchdog.BACKSTOP_DELAY + 2
    assert stopped.value.seconds == 0.2
    time.sleep(watchdog.BACKSTOP_DELAY + 0.3)  # a late signal would land here


def test_a_snippet_ending_in_time_is_never_hit_later():
    with watchdog.deadline(0.05):
        pass
    time.sleep(watchdog.BACKSTOP_DELAY + 0.3)  # the timer was cancelled


def test_an_except_exception_cannot_swallow_the_stop():
    now = [0.0]

    def stubborn():
        while True:
            with contextlib.suppress(Exception):  # what a probe or kit code may do
                now[0] += 1.0

    with pytest.raises(watchdog.ProbeTimeout), watchdog.deadline(3, clock=lambda: now[0]):
        stubborn()


@pytest.mark.parametrize("seconds", [None, 0])
def test_no_limit_installs_no_trace(seconds):
    before = sys.gettrace()
    with watchdog.deadline(seconds):
        assert sys.gettrace() is before
    assert sys.gettrace() is before


# -- nx_dev_python on the host -------------------------------------------------------


def test_a_snippet_past_its_limit_is_stopped_and_rolled_back(tmp_path):
    s = services(tmp_path)
    _snippet_host(s)
    before = sys.gettrace()
    code = "print('started')\nn = 0\nwhile True:\n    n += 1\n"
    with pytest.raises(NXError) as raised:
        dispatch.execute(s, "nx_dev_python", {"code": code, "timeout": 0.2,
                                              "allow_no_part": True})  # fmt: skip
    error = raised.value
    assert error.code == "NX_CANCELLED" and "0.2 s time limit" in error.message
    assert error.details["stopped_at"].startswith("<nx_dev_python>:")
    assert error.details["mutation_outcome"] == "rolled_back"
    assert error.details["stdout"] == "started\n" and "--timeout" in error.hint
    assert "<nx_dev_python>" in error.details["traceback"]
    assert sys.gettrace() is before


def test_a_snippet_within_its_limit_runs_untouched(tmp_path):
    s = services(tmp_path)
    _snippet_host(s)
    result = dispatch.execute(s, "nx_dev_python", {"code": "result = sum(range(1000))",
                                                   "allow_no_part": True})  # fmt: skip
    assert result["result"] == "499500" and result["status"] == "success"


def test_a_snippet_is_refused_without_a_work_part(tmp_path):
    s = services(tmp_path)
    _snippet_host(s)
    marks = s.session.next_mark
    with pytest.raises(NXError) as raised:
        interactive.InteractiveHost._dev_python(NS(session=s.session), "result = 1")
    error = raised.value
    assert error.code == "NX_NO_WORK_PART" and "Null tag not allowed" in error.message
    assert error.details["mutation_outcome"] == "not_started" and "--no-part" in error.hint
    assert s.session.next_mark == marks  # no undo mark: NX untouched


def test_the_guard_accepts_a_work_part_or_the_explicit_opt_in():
    with_part = NS(Parts=NS(Work=object()))
    no_part = NS(Parts=NS(Work=None))
    interactive.check_dev_python(with_part, {})
    interactive.check_dev_python(no_part, {"allow_no_part": True})
    with pytest.raises(NXError, match="work part"):
        interactive.check_dev_python(no_part, {"allow_no_part": False})


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"timeout": -1}, "timeout"),
        ({"timeout": bridge.DEV_PYTHON_TIMEOUT_MAX + 1}, "timeout"),
        ({"timeout": "60"}, "timeout"),
        ({"timeout": True}, "timeout"),
        ({"allow_no_part": "yes"}, "allow_no_part"),
    ],
)
def test_bad_snippet_arguments_are_refused(params, field):
    with pytest.raises(NXError) as raised:
        interactive.check_dev_python(NS(Parts=NS(Work=object())), params)
    assert raised.value.code == "NX_INVALID_ARGUMENT" and raised.value.field == field


def test_the_host_refuses_before_any_nx_call(monkeypatch):
    """execute() checks the snippet before the engineer-change check, the dialog watch
    and the call's undo mark: nothing touches NX."""
    host = interactive.InteractiveHost.__new__(interactive.InteractiveHost)
    host.thread = threading.get_ident()
    host._light, host.driver = True, "startup"
    host.session = NS(Parts=NS(Work=None))
    host.services = NS(dev_tools={"nx_dev_python": Mock()})
    host._check_engineer_change = Mock()
    run = Mock()
    monkeypatch.setattr(dispatch, "execute", run)
    with pytest.raises(NXError, match="work part"):
        host.execute("nx_dev_python", {"code": "result = 1"})
    host._check_engineer_change.assert_not_called()
    run.assert_not_called()


# -- the bridge waits for the snippet's own answer ---------------------------------------


def test_the_bridge_waits_past_the_snippets_own_limit():
    assert bridge.dev_python_seconds({}) == bridge.DEV_PYTHON_TIMEOUT
    assert bridge.dev_python_seconds({"timeout": 600}) == 600
    assert bridge.dev_python_seconds({"timeout": 0}) == 0
    assert bridge.dev_python_seconds({"timeout": "x"}) == bridge.DEV_PYTHON_TIMEOUT
    dispatcher = bridge.MainThreadDispatcher(lambda m, p: {}, run_timeout=bridge.run_timeout)

    def limit(method, params):
        return dispatcher._run_limit(bridge._PendingBridgeCall(method, params))

    margin = bridge.DEV_PYTHON_MARGIN
    assert limit("nx_dev_python", {}) == bridge.DEV_PYTHON_TIMEOUT + margin
    assert limit("nx_dev_python", {"timeout": 600}) == 600 + margin
    assert limit("nx_dev_python", {"timeout": 0}) == bridge.DISPATCHER_TIMEOUT
    assert limit("nx_test_edit", {"timeout": 600}) == bridge.DISPATCHER_TIMEOUT
    plain = bridge.MainThreadDispatcher(lambda m, p: {}, timeout=0.5)  # tests' fixed limit
    assert plain._run_limit(bridge._PendingBridgeCall("nx_dev_python", {})) == 0.5

"""A queued changing call past its start deadline (``expires_at``) whose client still
waits: answered NX_CALL_EXPIRED saying NX was busy and how many calls were queued before
it, retry ``after_wait``; never "the client gave up" (that is a closed connection)."""

import time

from tests.host.test_ui_recovery import _queue, host  # noqa: F401  (fixtures)


def test_a_late_call_is_answered_as_busy_not_as_given_up(host):  # noqa: F811
    queue = _queue(host, "ask")
    first = queue.submit("nx_set_camera", {"expires_at": time.time() + 60})
    late = queue.submit("nx_set_camera", {"expires_at": time.time() - 2})
    host.tick()
    error = late.error
    assert error.code == "NX_CALL_EXPIRED" and error.details["mutation_outcome"] == "not_started"
    assert "gave up" not in error.message and "did not run" in error.message
    assert error.retry == "after_wait" and error.details["queued_before"] == 1
    assert "1 call(s) were queued before it" in error.message
    assert not first.complete.is_set()  # the call ahead still waits for its card

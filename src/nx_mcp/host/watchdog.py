"""A cooperative time limit for development snippets (``nx_dev_python``).

A snippet runs on NX's UI thread, so nothing outside it can pre-empt it. :func:`deadline`
installs a ``sys.settrace`` hook on that thread for the snippet only: at each Python
call and line after the limit it raises :class:`ProbeTimeout`, so a Python loop stops
and the host rolls the snippet back. One blocking NX call (a native step that never
returns to Python) cannot be interrupted: the limit acts when it returns.

:class:`ProbeTimeout` is a ``BaseException``: an ``except Exception`` in the snippet or
in the code it calls cannot swallow it (Python also removes a trace hook whose call
raised, so a swallowed stop would let the loop run on). The hook is removed (the
previous one restored) when the block ends, whatever happens, so tools never run traced.
NX-free.
"""

from __future__ import annotations

import contextlib
import sys
import time
import traceback
from collections.abc import Callable, Iterator


class ProbeTimeout(BaseException):  # noqa: N818 - a stop signal, not an error class
    """The snippet ran past its time limit; ``where`` names the line it was at and
    ``stack`` the innermost frames."""

    def __init__(self, seconds: float, where: str, stack: str) -> None:
        super().__init__(f"stopped after {seconds:g} s at {where}")
        self.seconds = seconds
        self.where = where
        self.stack = stack


@contextlib.contextmanager
def deadline(
    seconds: float | None, *, clock: Callable[[], float] = time.monotonic
) -> Iterator[None]:
    """Stop Python code run in the block after ``seconds`` (None or 0: no limit)."""
    if not seconds:
        yield
        return
    end = clock() + float(seconds)

    def tracer(frame, event, arg):
        if clock() >= end:
            code = frame.f_code
            where = f"{code.co_filename}:{frame.f_lineno} in {code.co_name}"
            stack = "".join(traceback.format_stack(frame, limit=6))[-4000:]
            raise ProbeTimeout(float(seconds), where, stack)
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        yield
    finally:
        sys.settrace(previous)

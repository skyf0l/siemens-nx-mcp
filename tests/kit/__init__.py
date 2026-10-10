"""The shared fakes for kit and catalogue unit tests (architecture.md section 6).

``fake_nx(units)`` (``tests/kit/fake.py``) is the fake NX session;
``run(nx, tool, **args)`` (``tests/kit/kernel.py``) calls a catalogue tool on it
through the real kernel. The fake NX objects (``tests/kit/objects.py``) also back
the host ``rig`` fixture (``tests/kit/rig.py``).

``fake_nx`` gives builder factories that record configure/commit/``Destroy()``
(``last``, ``live_builders``, ``on_commit``, ``lacks``, ``refuse``), owned
geometry (``block``, ``face``, ``edge``, ``datum_plane``, ``datum_axis``,
``sketch``, ``curve``, ``expression``), drawing sheets and views, ``undo_marks``
and ``nx.tx``, a ``Tx``-shaped handle for calling kit helpers directly.
"""

from tests.kit.fake import FakeBuilder, FakeNX, FakeTx, fake_nx
from tests.kit.kernel import run
from tests.kit.objects import NXException, matrix, point

__all__ = ["FakeBuilder", "FakeNX", "FakeTx", "NXException", "fake_nx", "matrix", "point", "run"]

"""core/marks.py: load barriers and the reasons of dropped undo marks."""

from __future__ import annotations

from types import SimpleNamespace as NS

from nx_mcp.core import marks
from nx_mcp.core.tx import Services


class Marks(Services):
    def __init__(self):
        self.live = set()
        self.loaded = [NS(Tag=1, name="a.prt")]
        self.session = NS(Parts=NS(Display=self.loaded[0]))
        self.warned = []

    def parts(self):
        return list(self.loaded)

    def part_key(self, part):
        return f"part_{part.Tag}"

    def part_path(self, part):
        return part.name

    def mark_exists(self, mark):
        return mark in self.live

    def entry(self, mark):
        self.live.add(mark)
        self.undo_history().append({"mark": mark, "part_id": "part_1", "operation_id": f"op{mark}"})


def spec(effect, name="nx_tool"):
    return NS(effect=effect, name=name)


def tx(services):
    return NS(warn=lambda code, message: services.warned.append((code, message)))


def test_a_file_or_session_load_is_a_barrier_only_when_undo_could_reach_back():
    s = Marks()
    seen = marks.watch(s)
    s.loaded.append(NS(Tag=2, name="b.prt"))
    marks.settle(s, seen, spec("file", "nx_create_part"), "op_load", tx(s))
    assert s.undo_history() == []  # nothing to protect
    s.entry(10)
    for effect, loads in (("model", 1), ("session", 2), ("file", 3)):
        seen = marks.watch(s)
        s.loaded.append(NS(Tag=10 + loads, name=f"p{loads}.prt"))
        marks.settle(s, seen, spec(effect), f"op_{effect}", tx(s))
    barriers = [h for h in s.undo_history() if h.get("load") is not None]
    assert [b["load"] for b in barriers] == [["p2.prt"], ["p3.prt"]]
    assert barriers[0] == {"mark": None, "part_id": "part_12", "operation_id": "op_session",
                           "method": "nx_tool", "load": ["p2.prt"]}  # fmt: skip
    assert s.warned == []


def test_dropped_marks_get_the_reason_the_call_shows():
    for change, reason in (
        ("close", "saved_or_closed"),
        ("load", "part_loaded"),
        ("display", "display_reset"),
        ("save", "saved_or_closed"),
    ):
        s = Marks()
        s.entry(1)
        s.undo_checkpoints()["cp"] = {"mark": 2, "index": 1}
        s.live.add(2)
        seen = marks.watch(s)
        s.live.clear()
        if change == "close":
            s.loaded.clear()
        elif change == "load":
            s.loaded.append(NS(Tag=2, name="b.prt"))
        elif change == "display":
            s.session.Parts.Display = NS(Tag=9)
        marks.settle(s, seen, spec("session"), "op", tx(s))
        assert s.undo_history()[0]["expired"] == reason, change
        assert s.undo_checkpoints()["cp"]["expired"] == reason, change
        assert bool(s.warned) == (reason != "saved_or_closed"), change


def test_an_undo_removing_the_newest_entries_is_no_drop_and_eviction_is_noted():
    s = Marks()
    s.entry(1)
    s.entry(2)
    seen = marks.watch(s)
    s.live.discard(2)
    del s.undo_history()[-1]  # an undo took the newest operation off the history
    marks.settle(s, seen, spec("model"), "op_undo", tx(s))
    assert "expired" not in s.undo_history()[0] and s.warned == []
    s.entry(3)
    seen = marks.watch(s)
    s.live.discard(1)  # NX evicted the oldest mark
    marks.settle(s, seen, spec("model"), "op", tx(s))
    assert s.undo_history()[0]["expired"] == "mark_evicted"
    assert s.warned and "180 undo marks" in s.warned[0][1]


def test_settle_never_raises():
    s = Marks()
    s.entry(1)
    seen = marks.watch(s)
    s.parts = None  # type: ignore[assignment]
    marks.settle(s, seen, spec("file"), "op")
    marks.settle(s, None, spec("file"), "op")

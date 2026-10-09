"""Short references, kinds, anchors and staleness (architecture.md 1.3).

A ref is ``<prefix>:<n>`` (``b:41`` body, ``fc:88`` face, ...). ``n`` comes from
one workspace-wide counter that is never reused: its floor is persisted in
``.nx-mcp/refs.json`` when the host starts, so a ref from an earlier session or
reload fails as ``stale_reference`` and never aliases another object.

During migration every short ref is backed by an entry in the legacy
``ObjectRegistry`` (through the services adapter). Legacy invalidation (part
close, deleted objects, rolled-back parts, topology changes) therefore makes the
short spelling stale as well, and a legacy ``obj_...`` ID still resolves.

Features, bodies and sketches carry an automatic anchor built from the
persistent journal identifier (``@smoke/a/blk.prt:EXTRUDE(4)``, plus ``#<i>`` for
bodies); faces and edges only on request. A stale ref reports the anchor's
unique current match in ``details.current``; nothing is re-resolved silently.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.types import KIND_GROUPS, expand_kinds

PREFIX = {
    "part": "p",
    "body": "b",
    "feature": "f",
    "sketch": "s",
    "face": "fc",
    "edge": "e",
    "datum_plane": "d",
    "datum_axis": "d",
    "datum_csys": "d",
    "datum_point": "d",
    "component": "o",
    "curve": "c",
    "point": "pt",
    "expression": "x",
    "annotation": "a",
    "dimension": "dm",
    "drawing_sheet": "sh",
    "drawing_view": "v",
    "drawing_table": "t",
    "reference_set": "rs",
    "explosion": "ex",
    "traceline": "tl",
    "component_pattern": "cp",
    "assembly_constraint": "ac",
    "sketch_constraint": "sc",
    "section_view": "sv",
    "modeling_view": "mv",
}
ANCHORED = frozenset({"feature", "body", "sketch"})
# Legacy registry kinds -> exact kinds they may stand for.
LEGACY_KINDS = {"datum": KIND_GROUPS["datum"], "section": frozenset({"section_view"})}
# Exact kind -> kind name in the legacy registry (its kinds are coarser).
TO_LEGACY = {**dict.fromkeys(KIND_GROUPS["datum"], "datum"), "section_view": "section"}
_SHORT = re.compile(r"^([a-z]{1,3}):([1-9][0-9]{0,11})$")
_ANCHOR = re.compile(r"^@(?P<part>.+):(?P<jid>[^#]+?)(?:#(?P<index>\d+))?$")
BLOCK = 1000


class Counter:
    """Workspace-wide, never reused ref numbers with a persisted floor."""

    def __init__(self, path: str | Path | None = None, block: int = BLOCK) -> None:
        self.path = Path(path) if path else None
        self.block = block
        start = 1
        if self.path is not None and self.path.exists():
            try:
                start = max(1, int(json.loads(self.path.read_text("utf-8"))["next"]))
            except (OSError, ValueError, KeyError, TypeError):
                start = 1
        self.floor = start
        self.value = start
        self.limit = start
        self._reserve()

    def _reserve(self) -> None:
        self.limit = self.value + self.block
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps({"next": self.limit}), encoding="utf-8")
            os.replace(temp, self.path)

    def next(self) -> int:
        if self.value >= self.limit:
            self._reserve()
        number = self.value
        self.value += 1
        return number


@dataclass
class Entry:
    id: str
    kind: str
    legacy_id: str
    part_key: str
    part_id: str
    name: str
    anchor: str | None


def _tag(obj) -> str:
    tag = getattr(obj, "Tag", None)
    return str(tag if tag is not None else id(obj))


def _name(obj) -> str:
    name = getattr(obj, "Name", "")
    if callable(name):
        name = name()
    return str(name or getattr(obj, "JournalIdentifier", "") or "")


class References:
    """The ref table. ``backend`` is the services adapter (see ``core.tx.Services``)."""

    def __init__(self, backend, counter: Counter | None = None) -> None:
        self.backend = backend
        self.counter = counter or Counter()
        self._entries: dict[str, Entry] = {}
        self._identity: dict[tuple[str, str, str], str] = {}

    # -- rows ---------------------------------------------------------------
    def row(self, obj, kind: str, *, part=None, anchor: bool = False) -> dict:
        """``{id, kind, name, part}`` (+ ``anchor``) for a live NX object."""
        if kind not in PREFIX:
            fail("NX_INVALID_ARGUMENT", f"Unknown reference kind {kind!r}")
        part = part if part is not None else self.backend.owning_part(obj)
        part_key = self.backend.part_key(part)
        key = (part_key, kind, _tag(obj))
        entry = self._entries.get(self._identity.get(key, ""))
        if entry is not None and not self.backend.legacy_live(entry.legacy_id):
            entry = None
        if entry is None:
            number = self.counter.next()
            ref = f"{PREFIX[kind]}:{number}"
            legacy_id = self.backend.legacy_register(obj, TO_LEGACY.get(kind, kind), part)
            part_id = ref if kind == "part" else self.row(part, "part", part=part)["id"]
            entry = Entry(ref, kind, legacy_id, part_key, part_id, _name(obj), None)
            self._entries[ref] = entry
            self._identity[key] = ref
        else:
            entry.name = _name(obj)
        if entry.anchor is None and (kind in ANCHORED or anchor):
            entry.anchor = self._anchor(obj, kind, part)
        out = {"id": entry.id, "kind": kind, "name": entry.name, "part": entry.part_id}
        if entry.anchor and (kind in ANCHORED or anchor):
            out["anchor"] = entry.anchor
        return out

    def _anchor(self, obj, kind: str, part) -> str | None:
        jid = str(getattr(obj, "JournalIdentifier", "") or "")
        if not jid:
            return None
        anchor = f"@{self.backend.part_path(part)}:{jid}"
        if kind == "body":
            same = [b for b in self._collection(part, "body") if self._jid(b) == jid]
            index = next((i for i, b in enumerate(same) if _tag(b) == _tag(obj)), 0)
            anchor += f"#{index}"
        return anchor

    @staticmethod
    def _jid(obj) -> str:
        return str(getattr(obj, "JournalIdentifier", "") or "")

    @staticmethod
    def _collection(part, kind: str) -> list:
        name = {"feature": "Features", "body": "Bodies", "sketch": "Sketches"}.get(kind)
        return list(getattr(part, name, []) or []) if name else []

    # -- lookups ------------------------------------------------------------
    def entry(self, ref: str) -> Entry | None:
        return self._entries.get(ref)

    def name_of(self, ref: str) -> str | None:
        """Display name of a ref without touching NX (panel summaries)."""
        entry = self._entries.get(ref)
        if entry is not None:
            return entry.name or entry.kind
        if ref.startswith("obj_"):
            return self.backend.legacy_name(ref)
        return None

    def resolve(self, ref: str, kinds=()) -> object:
        """The live object for ``ref``; stale_reference / wrong_kind / not_found."""
        wanted = expand_kinds(kinds) if kinds else None
        if ref.startswith("obj_"):
            # Check the kind before resolving: obtaining a live object counts as touching NX.
            legacy_kind = self.backend.legacy_kind(ref)
            possible = LEGACY_KINDS.get(legacy_kind, frozenset({legacy_kind}))
            if legacy_kind is not None and wanted is not None and not (possible & wanted):
                self._wrong_kind(ref, legacy_kind, wanted)
            return self.backend.legacy_resolve(ref)
        match = _SHORT.match(ref)
        if not match:
            fail("NX_INVALID_ARGUMENT", f"Not a reference: {ref!r}")
        entry = self._entries.get(ref)
        if entry is None:
            if int(match.group(2)) < self.counter.floor:
                fail(
                    "NX_STALE_REFERENCE",
                    f"{ref} is from an earlier NX session or host reload",
                    details={"ref": ref},
                )
            fail("NX_NOT_FOUND", f"{ref} was never issued in this session", details={"ref": ref})
        if wanted is not None and entry.kind not in wanted:
            self._wrong_kind(ref, entry.kind, wanted)
        if not self.backend.legacy_live(entry.legacy_id):
            details: dict = {"ref": ref, "kind": entry.kind}
            if entry.anchor:
                details["anchor"] = entry.anchor
                current = self.find_anchor(entry.anchor, entry.kind)
                if current is not None:
                    details["current"] = current
            fail(
                "NX_STALE_REFERENCE",
                f"{ref} ({entry.name or entry.kind}) is no longer valid",
                details=details,
            )
        return self.backend.legacy_resolve(entry.legacy_id)

    @staticmethod
    def _wrong_kind(ref, kind, wanted):
        raise NXError(
            "NX_WRONG_KIND",
            f"{ref} is a {kind}; expected {'/'.join(sorted(wanted))}",
            details={"ref": ref, "kind": kind, "expected": sorted(wanted)},
        )

    def find_anchor(self, anchor: str, kind: str) -> dict | None:
        """The row of the unique current object matching ``anchor``, or None."""
        match = _ANCHOR.match(anchor or "")
        if not match:
            return None
        try:
            part = next(
                (p for p in self.backend.parts() if self.backend.part_path(p) == match["part"]),
                None,
            )
            if part is None:
                return None
            found = [o for o in self._collection(part, kind) if self._jid(o) == match["jid"]]
            if match["index"] is not None:
                index = int(match["index"])
                found = found[index : index + 1]
            if len(found) != 1:
                return None
            return self.row(found[0], kind, part=part)
        except Exception:
            return None

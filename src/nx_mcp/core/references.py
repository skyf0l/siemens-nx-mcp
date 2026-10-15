"""Short references, kinds, anchors and staleness (architecture.md 1.3).

A ref is ``<prefix>:<n>`` (``b:41`` body, ``fc:88`` face, ...). ``n`` comes from
one workspace-wide counter that is never reused: its floor is persisted in
``.nx-mcp/refs.json`` when the host starts, so a ref from an earlier NX session (or
one a host reload could not re-bind, see below) fails as ``stale_reference`` and
never aliases another object.

Every short ref is backed by a handle in the host's object registry (through the
services adapter, see ``core.tx.Services``). Registry invalidation (part close,
deleted objects, rolled-back parts) therefore makes the short ref stale as well. A
registry handle (``obj_...``) resolves too.

Refs follow NX's persistent object identity (the tag), not geometry. After a
committed call only the refs of objects NX no longer has go stale: deleted
features and bodies, consumed faces and edges. A face or edge a feature did not
consume keeps its ref, also when the feature reshaped it (NX 2406, measured: the
top face through a pocket, hole faces through threads, untouched edges through a
blend), so the ref names the same NX face, which may now have other bounds.

Features, bodies and sketches carry an automatic anchor built from the
persistent journal identifier (``@smoke/a/blk.prt:EXTRUDE(4)``, plus ``#<i>`` for
bodies); faces and edges only on request. An occurrence (a body a component shows)
is anchored by its component path and its prototype's identifier
(``@asm.prt:COMPONENT pin 1>EXTRUDE(2)#0``), a component by its path alone
(``@asm.prt:COMPONENT sub 1>COMPONENT pin 1``); an occurrence NX cannot name that way
gets no anchor. A body, face or edge anchor of ``nx_create_anchor``
(``@<part>:face=<handle>``) resolves too. An anchor resolves wherever a ref
does (the unique current object it names; one that matches several objects is
``NX_AMBIGUOUS_REFERENCE``, never one of them), and follows its part's new file after a
save-as. A stale ref reports the anchor's
unique current match in ``details.current``; nothing is re-resolved silently,
except after an undo, a rollback or an edit the agent did not make: NX 2406 undo
restores the same tags (measured for parts, bodies, features, sketches, faces and
edges), so refs of restored objects stay live, and a ref whose object came back
under a new tag is re-bound to its anchor's unique match
(:meth:`References.rebind`); only the rest go stale.

A development reload starts a new host in the same NX process: the old host's refs are
persisted in ``.nx-mcp/refs-live.json`` (:meth:`References.persist`) and re-bound on
first use when NX still has the same object (same tag, kind, part and journal
identifier) or their anchor's unique match; any other old ref is ``NX_STALE_REFERENCE``
saying so (list the objects again).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, TypedDict

from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.errors import article as article  # re-exported (the grammar helper)
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
# Kinds an anchor given as a ref may name: the automatic ones and components.
RESOLVABLE = ANCHORED | {"component"}
# Between the components of an occurrence anchor's path (``COMPONENT a 1>EXTRUDE(2)``).
PATH_SEP = ">"
# Registry kinds -> exact kinds they may stand for.
REGISTRY_KINDS = {"datum": KIND_GROUPS["datum"], "section": frozenset({"section_view"})}
# Exact kind -> kind name in the object registry (its kinds are coarser).
TO_REGISTRY = {**dict.fromkeys(KIND_GROUPS["datum"], "datum"), "section_view": "section"}
_SHORT = re.compile(r"^([a-z]{1,3}):([1-9][0-9]{0,11})$")
_ANCHOR = re.compile(r"^@(?P<part>.+):(?P<jid>[^#]+?)(?:#(?P<index>\d+))?$")
_INDEX = re.compile(r"#\d+$")  # a body anchor's index among bodies of one identifier
# nx_create_anchor's anchor of a body, face, edge or curve: ``@<part>:face=<NX handle>``.
HANDLE_ANCHOR = re.compile(r"^@(?P<part>.+?):(?P<kind>body|face|edge|curve)=(?P<handle>.+)$")
# NX's generic name of an unnamed object ("ENTITY 26 3 1"): not stable between reads.
GENERIC_NAME = re.compile(r"^ENTITY \d+ \d+ \d+$")
BLOCK = 1000
# Where a stale ref of a kind is found again (the hint of NX_STALE_REFERENCE).
REACQUIRE = {
    "part": "List the loaded parts with nx_list_parts for current part refs",
    "component": "List the components with nx_list_components for current refs",
    "face": "The face is gone (a feature consumed it, or an undo): find it again with "
    "nx_find_geometry",
    "edge": "The edge is gone (a feature consumed it, or an undo): find it again with "
    "nx_find_geometry",
}
REACQUIRE_DEFAULT = "List the objects again (nx_list_objects, nx_find_geometry) for current refs"
# Where refs of a kind come from (the hint for a ref this session never issued).
FINDERS = {
    "part": "nx_list_parts",
    "component": "nx_list_components",
    "face": "nx_find_geometry",
    "edge": "nx_find_geometry",
    "body": "nx_list_objects or nx_find_geometry",
}


class Row(TypedDict, total=False):
    """A reference row: ``{id, kind, name, part}`` (+ ``path`` for occurrences,
    ``anchor`` for features, bodies and sketches)."""

    id: str
    kind: str
    name: str
    part: str
    path: str
    anchor: str


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
    handle: str  # the object registry ID behind the ref
    part_key: str
    part_id: str
    name: str
    anchor: str | None
    tag: str = ""  # the NX tag the ref names (persisted for a host reload)
    jid: str = ""  # its journal identifier when the ref was made
    part_path: str = ""  # its part's workspace path when the ref was made
    full: str = ""  # its part's FullPath when the anchor was made (a save-as moves it)


def _tag(obj) -> str:
    tag = getattr(obj, "Tag", None)
    return str(tag if tag is not None else id(obj))


def deleted(obj) -> bool:
    """Whether ``obj`` wraps an object NX no longer has (a closed part, an object NX
    undid itself). NX 2406: such a wrapper reports tag 0 (NULL_TAG), its properties
    raise "Object no longer exists", and NX refuses it as an argument with "First
    parameter is invalid. Expecting int type, found str"."""
    try:
        tag = getattr(obj, "Tag", None)
        return tag is not None and int(tag) == 0
    except Exception:
        return False


def _gone(ref: str, kind: str | None, name: str | None):
    fail(
        "NX_STALE_REFERENCE",
        f"{ref} ({name or kind or 'object'}) no longer exists in NX",
        hint=REACQUIRE.get(kind or "", REACQUIRE_DEFAULT),
        details={"ref": ref, "kind": kind, "reason": "deleted_in_nx"},
    )


def _full(part) -> str:
    return str(getattr(part, "FullPath", "") or "")


def _prototype(obj):
    """The object an occurrence shows in its component's part (followed through nested
    occurrences), else None."""
    proto = obj
    for _ in range(32):
        nxt = getattr(proto, "Prototype", None)
        if nxt is None or nxt is proto:
            break
        proto = nxt
        if not getattr(proto, "IsOccurrence", False):
            return proto
    return None


def _raw_name(obj) -> str:
    name = getattr(obj, "Name", "")
    if callable(name):
        name = name()
    return str(name or getattr(obj, "JournalIdentifier", "") or "")


def _name(obj) -> str:
    """NX's name, else the journal identifier. An occurrence known only by its handle
    (``HANDLE R-722``) is ``<component>/<prototype name>``."""
    name = _raw_name(obj)
    if name.startswith("HANDLE ") and getattr(obj, "IsOccurrence", False):
        try:
            owner = str(getattr(obj.OwningComponent, "Name", "") or "")
            proto = _raw_name(obj.Prototype)
            if owner and proto and not proto.startswith("HANDLE "):
                return f"{owner}/{proto}"
        except Exception:
            pass
    return name


def _label(obj, kind: str, ref: str, name: str) -> str:
    """``name``; for NX's generic ``ENTITY n a b`` (it changes between reads and many
    objects share it) ``<kind> <ref>``, a sketch constraint with its constraint type."""
    if not GENERIC_NAME.match(name):
        return name
    label = kind.replace("_", " ")
    if kind == "sketch_constraint":
        with contextlib.suppress(Exception):
            ctype = str(obj.ConstraintType).rsplit(".", 1)[-1]
            if ctype and not ctype.isdigit():
                label = ctype.lower() + " constraint"
    return f"{label} {ref}"


class References:
    """The ref table. ``backend`` is the services adapter (see ``core.tx.Services``)."""

    def __init__(self, backend, counter: Counter | None = None, live_path=None) -> None:
        self.backend = backend
        self.counter = counter or Counter()
        self._entries: dict[str, Entry] = {}
        self._identity: dict[tuple[str, str, str], str] = {}
        # Per part: (revision, {body tag: (journal id, index among bodies of that id)}),
        # so the anchors of many bodies cost one pass over the part's bodies.
        self._body_order: dict[str, tuple] = {}
        self.version = 0  # moves when a ref is made or re-bound (:meth:`persist`)
        # Refs of an earlier host of this NX process (:meth:`persist`), until re-bound.
        self.live_path = Path(live_path) if live_path else None
        self._inherited: dict[str, dict] | None = None
        self._written: int | None = None

    # -- rows ---------------------------------------------------------------
    def row(self, obj, kind: str, *, part=None, anchor: bool = False) -> Row:
        """``{id, kind, name, part}`` (+ ``anchor``) for a live NX object."""
        if kind not in PREFIX:
            fail("NX_INVALID_ARGUMENT", f"Unknown reference kind {kind!r}")
        part = part if part is not None else self.backend.owning_part(obj)
        part_key = self.backend.part_key(part)
        key = (part_key, kind, _tag(obj))
        entry = self._entries.get(self._identity.get(key, ""))
        if entry is not None and not self.backend.handle_live(entry.handle):
            entry = None
        if entry is None:
            number = self.counter.next()
            ref = f"{PREFIX[kind]}:{number}"
            handle = self.backend.register_handle(obj, TO_REGISTRY.get(kind, kind), part)
            part_id = ref if kind == "part" else self.row(part, "part", part=part)["id"]
            entry = Entry(ref, kind, handle, part_key, part_id, "", None)
            entry.tag, entry.jid = _tag(obj), self._jid(obj)
            with contextlib.suppress(Exception):
                entry.part_path = str(self.backend.part_path(part))
            entry.full = _full(part)
            self._entries[ref] = entry
            self._identity[key] = ref
            self.version += 1
        else:
            # Same NX object (part, kind, tag): keep the registry on this live wrapper, so
            # a ref whose earlier wrapper NX invalidated works again after a fresh listing.
            entry.handle = self.backend.register_handle(obj, TO_REGISTRY.get(kind, kind), part)
        entry.name = _label(obj, kind, entry.id, _name(obj))
        full = _full(part)
        if full != entry.full:  # a save-as: the anchor names the part's new file
            entry.full, entry.anchor = full, None
            with contextlib.suppress(Exception):
                entry.part_path = str(self.backend.part_path(part))
        if entry.anchor is not None and self._moved(entry.anchor, obj, kind):
            entry.anchor = None  # NX renumbered the object (a feature reorder)
        if entry.anchor is None and (kind in ANCHORED or anchor):
            entry.anchor = self._anchor(obj, kind, part)
        out: Row = {"id": entry.id, "kind": kind, "name": entry.name, "part": entry.part_id}
        if entry.anchor and (kind in ANCHORED or anchor):
            out["anchor"] = entry.anchor
        return out

    def _anchor(self, obj, kind: str, part) -> str | None:
        if kind != "part" and getattr(obj, "IsOccurrence", False):
            return self._occurrence_anchor(obj, kind, part)
        jid = str(getattr(obj, "JournalIdentifier", "") or "")
        if not jid:
            return None
        anchor = f"@{self.backend.part_path(part)}:{jid}"
        if kind == "body":
            anchor += f"#{self._body_index(obj, jid, part)}"
        return anchor

    def _occurrence_anchor(self, obj, kind: str, part) -> str | None:
        """``@<assembly>:<component path>`` of a component; for a body, feature or sketch a
        component shows, the path then ``>`` and its prototype's identifier (``#i`` for a
        body). None when NX cannot name it that way: an occurrence's own journal
        identifier (``HANDLE R-626``) is the same for every occurrence of one object."""
        try:
            if kind == "component":
                path = self._component_path(obj, part)
                return f"@{self.backend.part_path(part)}:{PATH_SEP.join(path)}" if path else None
            if kind not in ANCHORED:
                return None
            proto = _prototype(obj)
            component = getattr(obj, "OwningComponent", None)
            jid = self._jid(proto) if proto is not None else ""
            if component is None or not jid or PATH_SEP in jid:
                return None
            path = self._component_path(component, part)
            if not path:
                return None
            anchor = f"@{self.backend.part_path(part)}:{PATH_SEP.join([*path, jid])}"
            if kind == "body":
                owner = getattr(proto, "OwningPart", None) or getattr(component, "Prototype", None)
                anchor += f"#{self._body_index(proto, jid, owner)}"
            return anchor
        except Exception:
            return None

    def _component_path(self, component, part) -> list[str] | None:
        """The journal identifiers of ``component`` and its parents below ``part``'s root
        component, outermost first; None when one is missing or holds the separator."""
        root = None
        with contextlib.suppress(Exception):
            root = part.ComponentAssembly.RootComponent
        out: list[str] = []
        node = component
        while node is not None and len(out) < 64:
            if root is not None and _tag(node) == _tag(root):
                break
            jid = self._jid(node)
            if not jid or PATH_SEP in jid:
                return None
            out.append(jid)
            node = getattr(node, "Parent", None)
        return out[::-1] or None

    def _walk(self, part, jids) -> list:
        """The components at the end of the path ``jids`` below ``part``'s root component:
        every match at each level (more than one: the path is ambiguous)."""
        try:
            nodes = [part.ComponentAssembly.RootComponent]
        except Exception:
            return []
        for jid in jids:
            nodes = [
                child
                for node in nodes
                if node is not None
                for child in list(node.GetChildren() or [])
                if self._jid(child) == jid
            ]
            if not nodes:
                break
        return nodes

    def _body_index(self, obj, jid: str, part) -> int:
        """``obj``'s position among the bodies of ``part`` with journal id ``jid``. One pass
        over the part's bodies serves every body until the review revision moves (a commit,
        an undo, an engineer's edit) or a body it does not know comes."""
        try:
            key = str(self.backend.part_key(part))
            revision = getattr(type(self.backend), "revision", None)
            # Services' default revision never moves: no cache then.
            own = revision is not None and revision.__qualname__ != "Services.revision"
            stamp = self.backend.revision() if own else None
        except Exception:
            key, stamp = "", None
        tag = _tag(obj)
        cached = self._body_order.get(key)
        if (
            stamp is None
            or cached is None
            or cached[0] != stamp
            or cached[1].get(tag, ("",))[0] != jid
        ):
            counts: dict[str, int] = {}
            order: dict[str, tuple[str, int]] = {}
            for body in self._collection(part, "body"):
                body_jid = self._jid(body)
                order[_tag(body)] = (body_jid, counts.get(body_jid, 0))
                counts[body_jid] = counts.get(body_jid, 0) + 1
            cached = (stamp, order)
            if stamp is not None:
                self._body_order = {key: cached}  # only the latest part is kept
        found = cached[1].get(tag)
        return found[1] if found is not None and found[0] == jid else 0

    @staticmethod
    def _moved(anchor: str, obj, kind: str | None = None) -> bool:
        """Whether ``obj``'s journal identifier differs from the one in ``anchor``. A journal
        identifier may hold ':' (a pattern instance body: ``Pattern Feature(3:5A:1A)``), so
        the anchor is compared by its end, never split at a colon. An occurrence compares
        its prototype's identifier, the end of its path."""
        if kind != "component" and getattr(obj, "IsOccurrence", False):
            obj = _prototype(obj) or obj
        jid = str(getattr(obj, "JournalIdentifier", "") or "")
        if not jid or not _ANCHOR.match(anchor):
            return False
        text = _INDEX.sub("", anchor)
        return not (text.endswith(":" + jid) or text.endswith(PATH_SEP + jid))

    def reanchor(self, ref: str) -> None:
        """Recompute ``ref``'s name and anchor from its object (after a rename)."""
        entry = self._entries.get(ref)
        if entry is None:
            return
        with contextlib.suppress(Exception):
            obj = self.backend.resolve_handle(entry.handle)
            part = self.backend.owning_part(obj)
            entry.name = _label(obj, entry.kind, entry.id, _name(obj))
            if entry.anchor is not None:
                entry.anchor = self._anchor(obj, entry.kind, part)

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
            return self.backend.handle_name(ref)
        return None

    def resolve(self, ref: str, kinds=()) -> object:
        """The live object for ``ref``; stale_reference / wrong_kind / not_found."""
        wanted = expand_kinds(kinds) if kinds else None
        if ref.startswith("@"):
            return self._resolve_anchor(ref, wanted)
        if ref.startswith("obj_"):
            # Check the kind before resolving: obtaining a live object counts as touching NX.
            kind = self.backend.handle_kind(ref)
            possible = REGISTRY_KINDS.get(kind, frozenset({kind}))
            if kind is not None and wanted is not None and not (possible & wanted):
                self._wrong_kind(ref, kind, wanted)
            obj = self.backend.resolve_handle(ref)
            if deleted(obj):
                _gone(ref, kind, self.backend.handle_name(ref))
            return obj
        match = _SHORT.match(ref)
        if not match:
            fail("NX_INVALID_ARGUMENT", f"Not a reference: {ref!r}")
        entry = self._entries.get(ref) or self._restore(ref)
        if entry is None:
            self._unknown(ref, match.group(1), int(match.group(2)))
        if wanted is not None and entry.kind not in wanted:
            self._wrong_kind(ref, entry.kind, wanted)
        if not self.backend.handle_live(entry.handle):
            details: dict = {"ref": ref, "kind": entry.kind}
            hint = REACQUIRE.get(entry.kind, REACQUIRE_DEFAULT)
            if entry.anchor:
                details["anchor"] = entry.anchor
                current = self.find_anchor(entry.anchor, entry.kind)
                if current is not None:
                    details["current"] = current
                    hint = f"Use details.current ({current['id']}), the same {entry.kind} now"
            if entry.kind == "part":
                with contextlib.suppress(Exception):
                    loaded = [self.row(p, "part", part=p) for p in self.backend.parts()]
                    details["loaded_parts"] = loaded[:20]
            fail(
                "NX_STALE_REFERENCE",
                f"{ref} ({entry.name or entry.kind}) is no longer valid",
                hint=hint,
                details=details,
            )
        obj = self.backend.resolve_handle(entry.handle)
        if deleted(obj):
            _gone(ref, entry.kind, entry.name)
        return obj

    def _unknown(self, ref: str, prefix: str, number: int) -> NoReturn:
        """Refuse a short ref this session never issued: an unknown prefix, a number
        issued with another prefix, a number not issued yet (never issued: NX_NOT_FOUND),
        or a number below the session's floor. Refs are not persisted, so the last cannot
        tell an earlier session's ref from an invented one: the message says both."""
        kinds = sorted(k for k, p in PREFIX.items() if p == prefix)
        if not kinds:
            fail(
                "NX_INVALID_ARGUMENT",
                f"{ref} is not a reference: no object kind has the prefix {prefix!r}",
                hint="Pass a ref exactly as a tool result returned it (b:41 body, fc:88 "
                "face, e:12 edge, f:7 feature, s:3 sketch, p:1 part)",
                details={"ref": ref},
            )
        finder = FINDERS.get(kinds[0], "nx_list_objects") if len(kinds) == 1 else "nx_list_objects"
        hint = (
            f"Get current refs from {finder} or from the result of the call that made the "
            "object; pass refs exactly as tool results return them"
        )
        if number < self.counter.floor and ref in (self._inherited or {}):
            fail(
                "NX_STALE_REFERENCE",
                f"{ref} is a reference from before the NX MCP host reload, and its object "
                "changed or is gone since: list the objects again",
                hint=hint,
                details={"ref": ref, "reason": "host_reloaded"},
            )
        if number >= self.counter.floor:
            other = next(
                (
                    e
                    for p in sorted(set(PREFIX.values()))
                    if (e := self._entries.get(f"{p}:{number}"))
                ),
                None,
            )
            if other is not None:
                fail(
                    "NX_NOT_FOUND",
                    f"{ref} was never issued: {number} is {other.id}, "
                    f"{article(other.kind)} {other.kind} ({other.name or other.kind})",
                    hint=f"Use {other.id} for that {other.kind}, or " + hint[0].lower() + hint[1:],
                    details={"ref": ref, "reason": "never_issued", "issued_as": other.id},
                )
            fail(
                "NX_NOT_FOUND",
                f"{ref} was never issued: no tool returned it in this NX session",
                hint=hint,
                details={"ref": ref, "reason": "never_issued"},
            )
        fail(
            "NX_STALE_REFERENCE",
            f"{ref} is not a reference of this NX session: a ref from before the last NX "
            "start or host reload no longer resolves, and one no tool returned never did",
            hint=hint,
            details={"ref": ref, "reason": "not_in_this_session"},
        )

    def _resolve_anchor(self, anchor: str, wanted) -> object:
        """The unique current object an anchor names: a feature, body, sketch or component
        (``@<part>:<journal id or component path>[#i]``, as result rows show), or the body,
        face or edge of a handle anchor (``@<part>:face=<handle>``, nx_create_anchor)."""
        handle = HANDLE_ANCHOR.match(anchor)
        if handle is not None:
            return self._resolve_handle_anchor(anchor, handle, wanted)
        if not _ANCHOR.match(anchor):
            fail("NX_INVALID_ARGUMENT", f"Not a reference or anchor: {anchor!r}")
        kinds = sorted(RESOLVABLE if wanted is None else wanted & RESOLVABLE)
        if not kinds:
            self._wrong_kind(anchor, "anchor of a feature, body, sketch or component", wanted)
        found = [(k, m) for k in kinds for m in self._anchor_candidates(anchor, k)]
        if len(found) > 1:
            fail(
                "NX_AMBIGUOUS_REFERENCE",
                f"{anchor} matches {len(found)} current objects, so it names none of them",
                hint="Pass the object's ref instead (nx_list_objects or nx_list_components "
                "list them)",
                details={"ref": anchor, "matches": len(found)},
            )
        if not found:
            fail(
                "NX_NOT_FOUND",
                f"{anchor} names no current {'/'.join(kinds)}",
                hint="Anchors name features, bodies, sketches and components; list them "
                "with nx_list_objects for their refs",
                details={"ref": anchor, "matches": 0},
            )
        kind, (obj, part) = found[0]
        self.row(obj, kind, part=part)  # a short ref and a live handle from now on
        return obj

    def _resolve_handle_anchor(self, anchor: str, match, wanted) -> object:
        """The body, face or edge of ``@<part>:<kind>=<handle>`` in a loaded part."""
        kind = match.group("kind")
        if wanted is not None and kind not in wanted:
            self._wrong_kind(anchor, kind, wanted)
        part = self._loaded_part(match.group("part"))
        obj = None
        if part is not None:
            with contextlib.suppress(Exception):
                tag = int(self.backend.uf.Tag.AskTagOfHandle(match.group("handle")))
                obj = self.backend.object_of_tag(tag) if tag else None
        if obj is None or deleted(obj):
            fail(
                "NX_STALE_REFERENCE",
                f"{anchor}: the anchored {kind} no longer exists"
                + ("" if part is not None else " in a loaded part"),
                hint="Select replacement geometry with nx_find_geometry",
                details={"ref": anchor, "kind": kind, "reason": "anchor_unresolved"},
            )
        if self.backend.part_key(self.backend.owning_part(obj)) != self.backend.part_key(part):
            fail(
                "NX_STALE_REFERENCE",
                f"{anchor}: the handle names an object of another part now",
                hint="Select replacement geometry with nx_find_geometry",
                details={"ref": anchor, "kind": kind, "reason": "anchor_unresolved"},
            )
        self.row(obj, kind, part=part)
        return obj

    @staticmethod
    def _wrong_kind(ref, kind, wanted):
        expected = "/".join(sorted(wanted))
        raise NXError(
            "NX_WRONG_KIND",
            f"{ref} is {article(kind)} {kind}; expected {expected}",
            details={"ref": ref, "kind": kind, "expected": sorted(wanted)},
        )

    def find_anchor(self, anchor: str, kind: str) -> Row | None:
        """The row of the unique current object matching ``anchor``, or None."""
        found = self._anchor_match(anchor, kind)
        if found is None:
            return None
        try:
            return self.row(found[0], kind, part=found[1])
        except Exception:
            return None

    def _anchor_match(self, anchor: str | None, kind: str):
        """``(object, part)`` of the unique current object matching ``anchor``, or None."""
        found = self._anchor_candidates(anchor, kind)
        return found[0] if len(found) == 1 else None

    def _anchor_candidates(self, anchor: str | None, kind: str) -> list:
        """``[(object, part)]`` of every current object of ``kind`` matching ``anchor``: one
        for a usable anchor, several when it is ambiguous (two components of one name)."""
        try:
            split = self._split_anchor(anchor)
            if split is None:
                return []
            part, jid, index = split
            if kind == "component":
                found = [] if index is not None else self._walk(part, jid.split(PATH_SEP))
                return [(c, part) for c in found if not deleted(c)]
            # Only a body anchor has ``#<i>`` (bodies of one feature share its journal id):
            # an anchor names a body, or a feature or sketch, never both.
            if (index is not None) != (kind == "body"):
                return []
            if PATH_SEP in jid:  # an occurrence: its component path, then its prototype
                *path, own = jid.split(PATH_SEP)
                out = []
                for component in self._walk(part, path):
                    protos = [
                        o
                        for o in self._collection(getattr(component, "Prototype", None), kind)
                        if self._jid(o) == own
                    ]
                    for proto in protos if index is None else protos[index : index + 1]:
                        occurrence = component.FindOccurrence(proto)
                        if occurrence is not None and not deleted(occurrence):
                            out.append((occurrence, part))
                return out
            found = [o for o in self._collection(part, kind) if self._jid(o) == jid]
            if index is not None:
                found = found[index : index + 1]
            return [(o, part) for o in found if not deleted(o)]
        except Exception:
            return []

    def _split_anchor(self, anchor: str | None):
        """``(part, journal id, index or None)`` of an anchor: the loaded part whose path
        (``@<path>:``, any case) starts it, the longest when several do; the rest is the
        journal identifier, which may itself hold ':'. None when no loaded part matches."""
        if not anchor or not _ANCHOR.match(anchor):
            return None
        index_match = _INDEX.search(anchor)
        text = _INDEX.sub("", anchor)
        index = int(index_match.group(0)[1:]) if index_match else None
        best = None
        for part in self.backend.parts():
            prefix = "@" + str(self.backend.part_path(part)) + ":"
            longer = best is None or len(prefix) > len(best[1])
            if longer and len(text) > len(prefix) and text.casefold().startswith(prefix.casefold()):
                best = (part, prefix)
        if best is None:
            return None
        return best[0], text[len(best[1]) :], index

    def rebind(self, retired, floor: int | None = None) -> dict:
        """Re-point short refs after an undo (architecture.md 1.3).

        ``retired`` are the registry handles the undo retired (objects that did not
        come back with the same tag). A ref on one of them whose anchor has exactly one
        current match of its kind keeps its number and points at that object; the
        others stay stale. ``floor``: refs numbered below it existed before the call
        (refs issued inside the failed call are not reported as stale).
        Returns ``{"rebound": [refs], "stale": [refs]}``."""
        retired = set(retired)
        rebound: list[str] = []
        stale: list[str] = []
        for entry in list(self._entries.values()):
            if entry.handle not in retired:
                continue
            found = self._anchor_match(entry.anchor, entry.kind) if entry.anchor else None
            if found is not None:
                obj, part = found
                try:
                    entry.handle = self.backend.register_handle(
                        obj, TO_REGISTRY.get(entry.kind, entry.kind), part
                    )
                except Exception:
                    found = None
                else:
                    self._identity[(entry.part_key, entry.kind, _tag(obj))] = entry.id
                    entry.tag = _tag(obj)
                    self.version += 1
                    rebound.append(entry.id)
                    continue
            number = int(entry.id.split(":", 1)[1])
            if floor is None or number < floor:
                stale.append(entry.id)
        return {"rebound": rebound, "stale": stale}

    def recheck_anchors(self, part_key: str, gone=()) -> list[dict]:
        """After features of ``part_key`` were deleted (an undo by deletion): NX may give
        later objects new journal identifiers, so an anchor could name another object.
        The anchors of deleted objects (``gone``: their tags, or a dead wrapper) are
        dropped, so no later re-bind takes a renumbered object for them; a live object
        whose identifier changed gets its new anchor. Returns ``[{ref, anchor, was}]``
        of the anchors that moved."""
        gone = {str(t) for t in gone}
        moved: list[dict] = []
        for entry in self._entries.values():
            if entry.part_key != part_key or entry.anchor is None:
                continue
            obj = None
            with contextlib.suppress(Exception):
                if self.backend.handle_live(entry.handle):
                    obj = self.backend.resolve_handle(entry.handle)
            if obj is None or deleted(obj) or _tag(obj) in gone:
                entry.anchor = None
                continue
            if self._moved(entry.anchor, obj, entry.kind):
                was = entry.anchor
                with contextlib.suppress(Exception):
                    entry.anchor = self._anchor(obj, entry.kind, self.backend.owning_part(obj))
                if entry.anchor != was:
                    moved.append({"ref": entry.id, "anchor": entry.anchor, "was": was})
        return moved

    def stale(self, retired, floor: int | None = None) -> list[str]:
        """Short refs whose registry handle is in ``retired``; refs numbered at or
        above ``floor`` were issued inside the call and are left out."""
        retired = set(retired)
        return [
            e.id
            for e in self._entries.values()
            if e.handle in retired and (floor is None or int(e.id.split(":", 1)[1]) < floor)
        ]

    # -- across a host reload ------------------------------------------------
    PERSIST_MAX = 50_000  # refs the table keeps for the next host

    def persist(self) -> None:
        """Write the live refs (``{ref: {kind, tag, jid, part, part_ref, name, anchor}}``)
        and those an earlier host of this NX process left that were not re-bound yet, so
        the next host (a development reload) resolves them (:meth:`_restore`). Writes
        only when the table changed; never raises."""
        if self.live_path is None:
            return
        with contextlib.suppress(Exception):
            inherited = self._load_inherited()
            if self._written == self.version and self.live_path.exists():
                return
            loaded = {str(self.backend.part_path(p)).casefold() for p in self.backend.parts()}
            rows = {r: v for r, v in inherited.items() if str(v.get("part")).casefold() in loaded}
            for ref, entry in self._entries.items():
                if not entry.tag or not self.backend.handle_live(entry.handle):
                    continue
                rows[ref] = {
                    "kind": entry.kind, "tag": entry.tag, "jid": entry.jid,
                    "part": entry.part_path, "part_ref": entry.part_id, "name": entry.name,
                    "anchor": entry.anchor,
                }  # fmt: skip
            if len(rows) > self.PERSIST_MAX:
                rows = dict(list(rows.items())[-self.PERSIST_MAX :])
            self.live_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.live_path.with_suffix(".tmp")
            temp.write_text(json.dumps({"pid": os.getpid(), "refs": rows}), encoding="utf-8")
            os.replace(temp, self.live_path)
            self._written = self.version

    def _load_inherited(self) -> dict[str, dict]:
        """The rows an earlier host of this NX process (same pid) persisted, read once."""
        if self._inherited is None:
            self._inherited = {}
            with contextlib.suppress(Exception):
                if self.live_path is not None and self.live_path.exists():
                    saved = json.loads(self.live_path.read_text(encoding="utf-8"))
                    if saved.get("pid") == os.getpid() and isinstance(saved.get("refs"), dict):
                        self._inherited = {
                            r: row
                            for r, row in saved["refs"].items()
                            if isinstance(row, dict) and r not in self._entries
                        }
        return self._inherited

    def _restore(self, ref: str) -> Entry | None:
        """Re-bind ``ref`` of an earlier host of this NX process (a development reload):
        the object with the persisted tag when it is still of the same kind, in the same
        part and has the same journal identifier, else the unique current match of its
        anchor. None when the ref was not persisted or its object is gone or changed."""
        row = self._load_inherited().get(ref)
        if row is None:
            return None
        try:
            kind = str(row["kind"])
            part = self._loaded_part(str(row.get("part") or ""))
            obj = self._by_tag(row, kind, part) if part is not None else None
            if obj is None and row.get("anchor") and kind in ANCHORED:
                found = self._anchor_match(str(row["anchor"]), kind)
                if found is not None:
                    obj, part = found
            if obj is None or part is None:
                return None
            part_key = self.backend.part_key(part)
            if kind == "part":
                part_id = ref
            else:
                part_ref = str(row.get("part_ref") or "")
                known = self._entries.get(part_ref)
                if known is None and part_ref and part_ref != ref:
                    known = self._restore(part_ref)
                part_id = known.id if known else self.row(part, "part", part=part)["id"]
            handle = self.backend.register_handle(obj, TO_REGISTRY.get(kind, kind), part)
        except Exception:
            return None
        entry = Entry(
            ref, kind, handle, part_key, part_id, str(row.get("name") or ""),
            row.get("anchor"), _tag(obj), self._jid(obj), str(row.get("part") or ""),
        )  # fmt: skip
        self._entries[ref] = entry
        self._identity.setdefault((part_key, kind, entry.tag), ref)
        self._load_inherited().pop(ref, None)
        self.version += 1
        return entry

    def _loaded_part(self, path: str):
        for part in self.backend.parts():
            if str(self.backend.part_path(part)).casefold() == path.casefold():
                return part
        return None

    def _by_tag(self, row: dict, kind: str, part):
        """The live object with the persisted tag, when it is still what the ref named."""
        tag = int(row["tag"])
        if kind == "part":
            return part if int(part.Tag) == tag else None
        lookup = getattr(self.backend, "object_of_tag", None)
        obj = lookup(tag) if lookup is not None else None
        if obj is None or deleted(obj):
            return None
        if self.backend.part_key(self.backend.owning_part(obj)) != self.backend.part_key(part):
            return None
        jid = str(row.get("jid") or "")
        if not jid or self._jid(obj) != jid:
            return None  # a reused tag, or an object NX renamed: not the same object
        inferred = None
        with contextlib.suppress(Exception):
            inferred = self.backend.infer_kind(obj, default=None)
        if inferred in PREFIX and inferred != kind and TO_REGISTRY.get(kind) != inferred:
            return None
        return obj

    def issued(self) -> int:
        """The next ref number: refs at or above it are issued after this point."""
        return self.counter.value

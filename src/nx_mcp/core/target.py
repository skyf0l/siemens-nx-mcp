"""Gate 3t: the loaded part a call targets (architecture.md 3.1).

Pure: reads the reference table and the loaded parts, changes nothing in NX (it may
issue a ``p:N`` ref for a part it reports). The call keys ``part`` and
``expires_at`` travel inside ``arguments`` like ``operation_id``
(:func:`split_call_keys`); :func:`check_call_keys` validates them.

Order (:func:`resolve`): the tool's own part argument (``target="arg:<param>"``), a
``resolver`` hook (it may return :data:`ANY`: the call's own object is gone, any part will
do because the handler only reports it), then the references' part (an occurrence pins its prototype's
owner, except under ``target="assembly"`` and for components, which pin the assembly)
and the ``part`` key (a loaded part's file name with or without ``.prt``, its workspace
path or ``p:N``; a ``part`` that contradicts the references is ``NX_WRONG_PART_CONTEXT``,
reads included), then the parent call's part (a redo's own calls), then the work part.

One rule, no mode and no hidden state: a call without refs and without ``part`` acts on
the work part while at most one of the agent's parts is loaded (:func:`agent_parts`:
the loaded parts the agent created or opened, or the engineer granted, components
included: the rows ``nx_list_parts`` marks ``owned``); with two or more it is
``NX_AMBIGUOUS_PART`` listing them. ``envelope.part`` names the target (its file) when
``part`` was given or two or more parts are loaded; the call that brings the agent's
count to two carries a note (:func:`load_note`).

Gate 4s (``core/switching.py``, phase P2) makes the target current. Services that cannot
switch keep phase P0's rule (:func:`refuse_other_part`): a call whose ``part`` names a part
that is not the work part is refused.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.types import REF_PATTERN, expand_kinds
from nx_mcp.core.validate import get_at, pointer

CALL_KEYS = ("part", "expires_at")
PART_TEXT_MAX = 260
LISTED = 10  # parts a hint or details list
# Reference kinds that decide the target when references span parts.
DECIDERS = {
    "assembly": frozenset(
        {"component", "assembly_constraint", "component_pattern", "explosion", "traceline"}
    ),
    "sheet": frozenset(
        {"drawing_sheet", "drawing_view", "dimension", "annotation", "drawing_table"}
    ),
}
EXAMPLE = '{"part": "<file name>"}'  # a neutral example: never a real loaded part
PART_HINT = "a loaded part's file name (with or without .prt), its workspace path or p:N"
LOAD_NOTE = "{count} parts are loaded: calls without refs now take part"
# A resolver's answer when the call's own object (a preview, a checkpoint, an operation)
# is gone or unknown: the call takes ``part`` or the work part, never NX_AMBIGUOUS_PART,
# and its handler says what happened to the object.
ANY = object()


@dataclass
class Target:
    """The part a call acts on and why (``source``: ``refs``, ``part``, ``arg``,
    ``resolver``, ``parent``, ``only``, ``work``, ``any``)."""

    part: Any
    key: str
    file: str  # the file name, e.g. "cover.prt"
    file_key: str  # the identity receipts fingerprint (workspace path, case folded)
    source: str
    loaded: int  # parts loaded when it was resolved (the engineer's too)
    field: str | None = None  # the argument that named it ("/part", "/edges/0")

    def row(self, services) -> dict:
        """``envelope.part``: ``{id, file, from}``."""
        return {"id": part_id(services, self.part), "file": self.file, "from": self.source}

    def reported(self, explicit: bool) -> bool:
        """Whether the envelope names the part: ``part`` given, or two or more parts
        loaded (the agent sees which one the call used)."""
        return explicit or self.loaded >= 2


# ---------------------------------------------------------------------------
# Call keys


def split_call_keys(spec, args) -> tuple[Any, dict]:
    """``(args, keys)``: the call keys of ``args`` that are not the tool's own parameters
    (``nx_close_part``'s ``part`` stays an argument)."""
    if not isinstance(args, dict):
        return args, {}
    keys = {k: args[k] for k in CALL_KEYS if k in args and k not in spec.params}
    if not keys:
        return args, {}
    return {k: v for k, v in args.items() if k not in keys}, keys


def check_call_keys(part=None, expires_at=None) -> float | None:
    """Refuse a malformed ``part``/``expires_at``; ``expires_at`` in epoch
    seconds (a number, or an ISO 8601 time with a zone)."""
    if part is not None and (
        not isinstance(part, str) or not part.strip() or len(part) > PART_TEXT_MAX
    ):
        fail(
            "NX_INVALID_ARGUMENT",
            f"/part: {PART_HINT} (at most {PART_TEXT_MAX} characters)",
            field="/part",
            hint=f"Pass part as a string, e.g. {EXAMPLE}",
        )
    if expires_at is None:
        return None
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        return float(expires_at)
    if isinstance(expires_at, str):
        with contextlib.suppress(ValueError):
            moment = datetime.fromisoformat(expires_at.strip().replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.timestamp()
    fail(
        "NX_INVALID_ARGUMENT",
        "/expires_at: seconds since 1970 (UTC) or an ISO 8601 time such as 2026-10-11T12:00:00Z",
        field="/expires_at",
    )


def check_expiry(spec, expires_at: float | None, now: float | None = None) -> None:
    """The ghost guard: a changing call whose client gave up (``expires_at`` passed) never
    runs (reads still answer)."""
    if expires_at is None or spec.effect == "read":
        return
    late = (time.time() if now is None else now) - expires_at
    if late > 0:
        fail(
            "NX_CALL_EXPIRED",
            f"The call expired {late:.0f} s before NX could run it: its client gave up, "
            "so it did not run",
            hint="Send the call again if it is still wanted (nx_get_operation shows what ran)",
            details={"expires_at": expires_at, "late_seconds": round(late, 3)},
            retry="after_fix",
        )


# ---------------------------------------------------------------------------
# Loaded parts


def file_name(part) -> str:
    full = str(getattr(part, "FullPath", "") or "")
    if full:
        return Path(full.replace("\\", "/")).name
    return f"{getattr(part, 'Name', '') or 'part'}.prt"


def file_key(services, part) -> str:
    """The part's file identity for receipts (workspace path where it lies inside)."""
    try:
        path = str(services.part_path(part))
    except Exception:
        path = str(getattr(part, "FullPath", "") or file_name(part))
    return path.replace("\\", "/").casefold()


def part_id(services, part) -> str | None:
    with contextlib.suppress(Exception):
        return services.references.row(part, "part")["id"]
    return None


def same(services, a, b) -> bool:
    if a is None or b is None:
        return False
    try:
        return services.part_key(a) == services.part_key(b)
    except Exception:
        return a is b


def part_by_key(services, key) -> Any:
    """The loaded part whose ``part_key`` is ``key``, else None (a closed part)."""
    if not key:
        return None
    with contextlib.suppress(Exception):
        for part in services.parts() or []:
            with contextlib.suppress(Exception):
                if services.part_key(part) == key:
                    return part
    return None


def roots(services) -> list:
    """The top-level loaded parts (no loaded assembly uses them as a component)."""
    finder = getattr(services, "root_parts", None)
    if finder is not None:
        with contextlib.suppress(Exception):
            return list(finder())
    return list(services.parts() or [])


def counts(services, part) -> bool:
    """Whether ``part`` is one of the agent's parts: created or opened by the agent, or
    granted by the engineer (``nx_list_parts`` ``owned``); never a part the engineer
    opened. Unknown counts."""
    try:
        return bool(services.owns(part))
    except Exception:
        return True


def agent_parts(services, loaded=None) -> list:
    """The agent's loaded parts (:func:`counts`), components included, in load order."""
    loaded = list(services.parts() or []) if loaded is None else list(loaded)
    return [p for p in loaded if counts(services, p)]


def _tag(part) -> int | None:
    try:
        return int(part.Tag)
    except Exception:
        return None


def census(services) -> tuple[frozenset, int]:
    """Before a changing call: ``(tags of the loaded parts, the agent's count)``."""
    try:
        loaded = list(services.parts() or [])
    except Exception:
        return frozenset(), 2  # unknown: no note
    return frozenset(_tag(p) for p in loaded), len(agent_parts(services, loaded))


def load_note(services, before: tuple[frozenset, int] | None) -> str | None:
    """After a committed call: the note when it brought the agent's loaded parts from
    fewer than two to two or more. A part the call loaded is the agent's (the host owns
    it right after the call, ``Services.track``)."""
    if before is None or before[1] >= 2:
        return None
    tags = before[0]
    try:
        loaded = list(services.parts() or [])
    except Exception:
        return None
    count = sum(1 for p in loaded if _tag(p) not in tags or counts(services, p))
    return LOAD_NOTE.format(count=count) if count >= 2 else None


def rows(services, parts) -> list[dict]:
    """``[{id, file, work?, display?}]`` of ``parts`` (at most ``LISTED``)."""
    work = services.work_part()
    display = getattr(getattr(services.session, "Parts", None), "Display", None)
    out = []
    for part in list(parts)[:LISTED]:
        row: dict = {"id": part_id(services, part), "file": file_name(part)}
        if same(services, part, work):
            row["work"] = True
        if same(services, part, display):
            row["display"] = True
        out.append(row)
    return out


def _names(parts) -> str:
    return ", ".join(file_name(p) for p in list(parts)[:LISTED])


def part_from_text(services, text: str, field: str = "/part"):
    """The loaded part ``text`` names: ``p:N``, a workspace path, or a file name with or
    without ``.prt`` (``bracket.v2`` is ``bracket.v2.prt``, never ``bracket.prt``)."""
    loaded = list(services.parts() or [])
    text = text.strip()
    if re.match(REF_PATTERN, text):
        try:
            return services.references.resolve(text, ("part",))
        except NXError as error:
            error.field = field
            error.details.setdefault("loaded_parts", rows(services, loaded))
            if error.category == "stale_reference":
                error.hint = (
                    f"That part is closed; use a loaded file name: {_names(loaded)} "
                    f"(e.g. {EXAMPLE}) or p:N from nx_list_parts"
                )
            raise
    name = text if text.casefold().endswith(".prt") else text + ".prt"
    if re.search(r"[/\\:]", text):  # a path: inside the workspace only, never by stem
        try:
            wanted = Path(services.workspace.resolve(name)).resolve()
        except Exception as error:
            fail(
                "NX_PATH_OUTSIDE_WORKSPACE",
                f"{field}: {error}",
                field=field,
                hint=f"Name the part by its file name: {_names(loaded)} (e.g. {EXAMPLE})",
                details={"part": text, "loaded_parts": rows(services, loaded)},
            )
        found = [p for p in loaded if _same_file(p, wanted)]
    else:
        found = [p for p in loaded if file_name(p).casefold() == name.casefold()]
    if len(found) == 1:
        return found[0]
    if found:
        fail(
            "NX_AMBIGUOUS_PART",
            f"{field}: {len(found)} loaded parts have the file {name}",
            field=field,
            hint="Pass the part's workspace path or its p:N from nx_list_parts",
            details={"part": text, "loaded_parts": rows(services, found)},
        )
    fail(
        "NX_NOT_FOUND",
        f"{field}: no loaded part has the file {name}",
        field=field,
        hint=f"Use a loaded file name: {_names(loaded) or 'none is loaded'}"
        + (f" (e.g. {EXAMPLE})" if loaded else ""),
        details={"part": text, "loaded_parts": rows(services, loaded)},
    )


def _same_file(part, wanted: Path) -> bool:
    full = str(getattr(part, "FullPath", "") or "")
    if not full:
        return False
    try:
        return str(Path(full).resolve()).casefold() == str(wanted).casefold()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# References


def _kind_of(services, obj, kinds) -> str | None:
    kind_of = getattr(services.references, "kind_of", None)
    with contextlib.suppress(Exception):
        kind = kind_of(obj) if kind_of is not None else None
        if kind:
            return kind
    with contextlib.suppress(Exception):
        kind = services.infer_kind(obj)
        if isinstance(kind, str):
            return kind
    return next(iter(sorted(expand_kinds(kinds))), None) if kinds else None


def owner_of(services, obj, kind: str | None, policy: str):
    """The part an object pins: a part itself; an occurrence its prototype's owner
    (``assembly`` policy and components: the assembly that holds it)."""
    if kind == "part" or hasattr(obj, "PartUnits"):
        return obj
    if getattr(obj, "IsOccurrence", False) and policy != "assembly" and kind != "component":
        prototype = getattr(obj, "Prototype", None)
        owner = getattr(prototype, "OwningPart", None)
        if owner is not None:
            return owner
    return services.owning_part(obj)


def pinned_parts(services, checked, policy: str) -> dict:
    """``{part_key: (part, [fields], {kinds}, {holder keys})}`` for every resolvable
    reference argument, in argument order (an unknown or stale ref is the handler's to
    report). The holder keys are the assemblies that hold an occurrence a ref names: a
    ``part`` may name them as well."""
    pinned: dict = {}
    for path, kinds in checked.refs:
        value = get_at(checked.args, path)
        if not isinstance(value, str) or value.startswith("$"):
            continue
        try:
            obj = services.references.resolve(value, kinds)
            kind = _kind_of(services, obj, kinds)
            owner = owner_of(services, obj, kind, policy)
            key = services.part_key(owner)
        except Exception:
            continue
        entry = pinned.setdefault(key, (owner, [], set(), set()))
        entry[1].append(pointer(path))
        entry[2].add(kind)
        if getattr(obj, "IsOccurrence", False):
            with contextlib.suppress(Exception):
                holder = services.owning_part(obj)
                if holder is not None:
                    entry[3].add(services.part_key(holder))
    return pinned


# ---------------------------------------------------------------------------
# The gate


def _target(services, part, source: str, loaded: int, field=None) -> Target:
    return Target(
        part, services.part_key(part), file_name(part), file_key(services, part), source,
        loaded, field,
    )  # fmt: skip


def resolve(spec, checked, services, *, part: str | None = None, parent=None) -> Target | None:
    """Gate 3t: the call's :class:`Target`, or None for a call without one (``none``,
    ``creates``, a ``new_part`` call, an unnamed part argument, nothing loaded). A call
    that names nothing is ``NX_AMBIGUOUS_PART`` when two or more of the agent's parts
    are loaded, else it acts on the work part (source ``only``/``work``)."""
    policy = spec.target
    args = checked.args
    if spec.runs != "nx" or policy in ("none", "creates"):
        return None
    if spec.new_part is not None and args.get(spec.new_part) is not None:
        return None  # the call works in a part it creates
    loaded = list(services.parts() or [])
    if not loaded:
        return None  # gate 4: no work part
    count = len(loaded)
    if policy.startswith("arg:"):
        own = args.get(policy[4:])
        if isinstance(own, str) and own.strip():
            with contextlib.suppress(NXError):  # the handler reports a bad part argument
                return _target(services, part_from_text(services, own), "arg", count)
            return None
        return _fallback(spec, services, loaded, parent)
    if policy == "any":  # an NX-global read: any loaded part will do, never ambiguous
        work = services.work_part()
        chosen = part_from_text(services, part) if part is not None else work or loaded[0]
        return _target(services, chosen, "any", count, "/part" if part is not None else None)
    explicit = part_from_text(services, part) if part is not None else None
    if policy == "resolver" and spec.target_resolver is not None:
        found = None
        with contextlib.suppress(Exception):
            found = spec.target_resolver(services, args)
        if found is ANY:
            if explicit is not None:
                return _target(services, explicit, "part", count, "/part")
            return _target(services, services.work_part() or loaded[0], "any", count)
        if found is not None:
            if explicit is not None and not same(services, explicit, found):
                fail(
                    "NX_WRONG_PART_CONTEXT",
                    f"/part names {file_name(explicit)} but the call's own object belongs to "
                    f"{file_name(found)}",
                    field="/part",
                    hint=f"Drop part, or pass part {file_name(found)}",
                    details={"part": rows(services, [explicit])[0], "own": rows(services, [found])[0]},
                )  # fmt: skip
            return _target(services, found, "resolver", count)
    pinned = pinned_parts(services, checked, policy)
    if explicit is not None:
        key = services.part_key(explicit)
        if pinned and not any(key == k or key in v[3] for k, v in pinned.items()):
            other, fields, _kinds, _holders = next(iter(pinned.values()))
            fail(
                "NX_WRONG_PART_CONTEXT",
                f"/part names {file_name(explicit)} but {fields[0]} names objects of "
                f"{file_name(other)}",
                field="/part",
                hint=f"Drop part (the refs pick {file_name(other)}), or pass refs of "
                f"{file_name(explicit)}",
                details={"part": rows(services, [explicit])[0], "refs_part": rows(services, [other])[0]},
            )  # fmt: skip
        return _target(services, explicit, "part", count, "/part")
    if len(pinned) == 1:
        owner, fields, _kinds, _holders = next(iter(pinned.values()))
        return _target(services, owner, "refs", count, fields[0])
    if len(pinned) > 1:
        deciders = DECIDERS.get(policy, frozenset())
        decided = [v for v in pinned.values() if v[2] & deciders]
        if len(decided) == 1:
            return _target(services, decided[0][0], "refs", count, decided[0][1][0])
        if spec.effect == "read":  # a measurement across parts: the first ref's part
            owner, fields, _kinds, _holders = next(iter(pinned.values()))
            return _target(services, owner, "refs", count, fields[0])
        work = services.work_part()
        if work is not None and (
            services.part_key(work) in pinned or len(agent_parts(services, loaded)) < 2
        ):
            return _target(services, work, "work", count)  # as before
        listed = [{**rows(services, [v[0]])[0], "fields": v[1]} for v in pinned.values()]
        fail(
            "NX_AMBIGUOUS_PART",
            "The references name objects of several parts: "
            + ", ".join(f"{r['fields'][0]} in {r['file']}" for r in listed[:LISTED]),
            field=listed[0]["fields"][0],
            hint="Add part to the arguments to say which part the call changes, e.g. "
            f"{EXAMPLE}: one of {', '.join(r['file'] for r in listed[:LISTED])}",
            details={"parts": listed[:LISTED]},
        )
    if parent is None and _named_refs(checked):  # unknown or stale refs: the handler says so
        work = services.work_part()
        if work is not None:
            return _target(services, work, "work", count)
    return _fallback(spec, services, loaded, parent)


def _named_refs(checked) -> bool:
    """Whether the call passes any reference (resolvable or not): it is not ref-less."""
    for path, _kinds in checked.refs:
        value = get_at(checked.args, path)
        if isinstance(value, str) and not value.startswith("$"):
            return True
    return False


def _into_assembly(spec) -> bool:
    """An assembly call that reads another part file (``nx_create_component``): its
    ``part`` is the assembly that receives the component, never the component."""
    paths = getattr(spec, "paths", None) or ()
    return spec.target == "assembly" and any(
        mode == "read" and ".prt" in suffixes for _where, mode, suffixes in paths
    )


def _fallback(spec, services, loaded: list, parent) -> Target | None:
    count = len(loaded)
    if parent is not None and parent.part is not None:
        return Target(
            parent.part, parent.key, parent.file, parent.file_key, "parent", count, parent.field
        )
    mine = agent_parts(services, loaded)
    work = services.work_part()
    if len(mine) <= 1:  # at most one of the agent's parts: the work part, as before
        chosen = work if work is not None else next(iter(roots(services)), None)
        source = "only" if count <= 1 else "work"
        return _target(services, chosen, source, count) if chosen is not None else None
    ordered = sorted(mine, key=lambda p: same(services, p, work))  # the work part last
    names = _names(ordered)
    if _into_assembly(spec):
        hint = (
            "Add the assembly's file name to the arguments (the part that receives the "
            f'component, not the component\'s file), e.g. {{"part": "<assembly file name>"}}; '
            f"loaded: {names}"
        )
    else:
        hint = (
            f"Add the target part's file name to the arguments, e.g. {EXAMPLE}; loaded: "
            f"{names} (nx_list_parts lists them)"
        )
    fail(
        "NX_AMBIGUOUS_PART",
        f"{len(mine)} parts are loaded and {spec.name} has no refs that pick one",
        hint=hint,
        details={"loaded_parts": rows(services, ordered), "count": len(mine)},
    )


def refuse_other_part(spec, target: Target | None, services, *, explicit: bool) -> None:
    """For services that cannot switch (gate 4s): a call whose ``part`` names a part that
    is not the work part is refused, so it never acts on the wrong part. A call that names
    no part behaves as before (a ref of another part: the tool's own NX_WRONG_OWNER)."""
    if target is None or target.source in ("arg", "any") or not explicit:
        return
    work = services.work_part()
    if work is None or same(services, work, target.part):
        return
    here = file_name(work)
    fail(
        "NX_WRONG_PART_CONTEXT",
        f"{spec.name} targets {target.file} but the work part is {here}; NX MCP does not "
        "switch parts on its own yet",
        field=target.field,
        hint=f'Make {target.file} the work part with nx_activate_part(part="'
        f'{part_id(services, target.part)}"), then repeat the call',
        details={"part": target.row(services), "work_part": rows(services, [work])[0]},
    )

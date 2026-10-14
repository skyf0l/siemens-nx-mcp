"""Part ownership between callers: engineer parts, other agents' parts, busy parts (3.4).

**Caller identity.** The sidecar adds the call key ``caller`` to every NX call
(``core/target.check_caller``): ``{"id", "shared"}``. ``id`` names the sidecar process and
its MCP session (plus an id the MCP client puts in the request's ``_meta``, when it gives
one): one per agent where each agent has its own sidecar. ``shared`` is true while that
connection ran calls at the same time within the last two minutes: several agents share
it (subagents of one client share its MCP server), so ``id`` does not tell them apart.
A call without the key (an older sidecar, a direct bridge client) has no identity.

**Owners.** A committed changing call that loads a part (create, open, import, an
assembly's components) records the loading caller (:func:`record`). The record survives
a host reload in the same NX process (``.nx-mcp/part-owners.json``: tag and file of every
loaded agent part, :func:`adopt`). The file is rewritten whenever the agent's parts change:
after every committed changing call and after the host tracks the parts a call loaded
(``Services.track``: development snippets and failed calls included), and the parts an
earlier host generation listed are adopted before it is rewritten. A part matches its row
by tag *and* file: a part the engineer closed and opened again (a new tag) is theirs.

**Rules** (:func:`refuse_engineer`, :func:`refuse_close`, :func:`refuse_activate`):

- An *engineer part* (loaded by the engineer: not created or opened by an agent call;
  ``Services.created``) is never closed or activated by an agent tool, and never saved
  unless the engineer granted it on the panel (``NX_ENGINEER_PART``, no ``force``,
  development mode included). Read tools still read it.
- *Another agent's part* (its owner's id and the caller's id are both known and differ)
  is not closed or activated without ``force`` (``NX_PART_BUSY``, reason
  ``other_agent``). Saving it loses nothing and is not refused.
- *No identity tells the callers apart* (same id, or either unknown). A close is refused
  only when all hold: the caller's connection is ``shared``; the close discards changes
  (``save`` false); the part has unsaved changes a call made within ``BUSY_SECONDS``; and
  a call worked in it within ``RECENT_SECONDS`` (reason ``recent_change``). A solo agent
  (its connection never runs calls at once) is never refused by this rule, nor is a close
  that saves. In a shared connection an agent's own close within ``RECENT_SECONDS`` of
  its own work in the part is refused too (one retry with ``force``): the refusal says
  so. Measured with many agents in one sidecar: the foreign close of a part changed 17 s and
  worked in 0 s before is refused; own closes 2 to 56 s after own edits mostly pass.
- Closing an assembly keeps a component part that a call worked in (saves included)
  within ``BUSY_SECONDS`` after the assembly was loaded (:func:`component_reason`).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from nx_mcp.core import target as targets
from nx_mcp.core.errors import fail

BUSY_SECONDS = 300.0  # unsaved changes this recent make a part busy
RECENT_SECONDS = 30.0  # ... when a call also worked in it this recently (shared connection)
CHANGING = ("model", "file", "session")  # calls that stamp the parts they worked in
CHANGES = ("model", "session")  # calls that change their part (file calls never do)
SAVE = "nx_save_part"  # its part's changes are on disk: not busy until the next change
OWNERS_FILE = "part-owners.json"  # in <workspace>/.nx-mcp: agent parts across host reloads


def _table(services) -> dict:
    try:
        return services.part_activity()
    except Exception:
        return {}


def _entries(services) -> dict:
    return _table(services).setdefault("parts", {})


def _owners(services) -> dict:
    """``{part_key: {"caller": id or None, "at": load time}}``."""
    return _table(services).setdefault("owners", {})


def _tag(part) -> int | None:
    try:
        return int(part.Tag)
    except Exception:
        return None


def _caller_id(caller) -> str | None:
    return caller.get("id") if isinstance(caller, dict) else None


# ---------------------------------------------------------------------------
# Recording


def record(services, spec, target, tx, loaded_before: frozenset | None, now=None) -> None:
    """After a committed changing call: stamp the parts it worked in and record the caller
    of the parts it loaded. ``loaded_before``: the tags of the parts loaded before the
    call. Never raises."""
    if spec.effect not in CHANGING:
        return
    with contextlib.suppress(Exception):
        _record(services, spec, target, tx, loaded_before, time.time() if now is None else now)


def _record(services, spec, target, tx, loaded_before, now) -> None:
    entries = _entries(services)
    operation_id = getattr(tx, "operation_id", None)
    caller = _caller_id(getattr(tx, "caller", None))
    changes = spec.effect in CHANGES

    def stamp(
        key, *, worked: bool, loaded: bool = False, made: bool = False, saved: bool = False
    ) -> None:
        entry = entries.setdefault(key, {})
        if worked:
            # worked in (component_reason, RECENT_SECONDS): saves and exports too
            entry.update(at=now, tool=spec.name, operation_id=operation_id)
            if saved or (spec.name == SAVE and not made):
                entry["saved_at"] = now
            elif changes or made:
                entry["changed_at"] = now
                entry["change"] = {"tool": spec.name, "operation_id": operation_id}
        if loaded:
            entry["loaded_at"] = now
            _owners(services)[key] = {"caller": caller, "at": now}

    if target is not None:
        stamp(target.key, worked=True)
    for key in getattr(tx, "extra_parts", None) or ():  # batch children in other parts
        stamp(key, worked=True)
    worked_in = getattr(tx, "worked_in", None)
    new = None
    if worked_in is not None:  # a part the call made (unsaved) or opened (as its file)
        key = services.part_key(worked_in)
        new = loaded_before is not None and _tag(worked_in) not in loaded_before
        # nx_open_part loading the file (nx_create_part is a file call; a reused part
        # keeps what it was)
        opened = spec.effect == "session" and new
        stamp(key, worked=True, made=not opened, saved=opened)
    if loaded_before is not None:
        loaded = [p for p in services.parts() or [] if _tag(p) not in loaded_before]
        for part in loaded:
            stamp(services.part_key(part), worked=False, loaded=True)
        if target is None and worked_in is None and loaded:
            stamp(services.part_key(loaded[0]), worked=True, made=True)
    for key in [k for k, e in entries.items() if now - _newest(e) > 2 * BUSY_SECONDS]:
        entries.pop(key, None)
    owners = _owners(services)
    live = {services.part_key(p) for p in services.parts() or []}
    for key in [k for k in owners if k not in live]:  # closed parts
        owners.pop(key, None)
    persist(services)


def _newest(entry: dict) -> float:
    return max(float(entry.get("at") or 0.0), float(entry.get("loaded_at") or 0.0))


def _age(entry: dict | None, now: float) -> float | None:
    """Seconds since a call worked in the part, None when that is longer ago than
    ``BUSY_SECONDS``."""
    if not entry or entry.get("at") is None:
        return None
    age = now - float(entry["at"])
    return age if age <= BUSY_SECONDS else None


def _unsaved_age(entry: dict | None, now: float) -> float | None:
    """Seconds since a call changed the part when no save came after the change and it
    is at most ``BUSY_SECONDS`` ago; None otherwise (not busy)."""
    if not entry or entry.get("changed_at") is None:
        return None
    changed = float(entry["changed_at"])
    if float(entry.get("saved_at") or 0.0) >= changed:
        return None
    age = now - changed
    return age if age <= BUSY_SECONDS else None


# ---------------------------------------------------------------------------
# Owners across host reloads


def _owners_path(services) -> Path | None:
    with contextlib.suppress(Exception):
        return Path(services.workspace.root) / ".nx-mcp" / OWNERS_FILE
    return None


def _file(part) -> str:
    return str(getattr(part, "FullPath", "") or "").replace("\\", "/").casefold()


def persist(services) -> None:
    """Write the tag, file and caller of every loaded agent part (never raises).

    Adopts the rows of an earlier host generation first (a first call after a reload
    must not drop them) and writes only when the rows changed."""
    path = _owners_path(services)
    if path is None:
        return
    adopt(services)
    with contextlib.suppress(Exception):
        owners = _owners(services)
        rows = {}
        for part in services.parts() or []:
            key, tag = services.part_key(part), _tag(part)
            # a part this call loaded is owned only after it (``Services.track``)
            if tag is None or (key not in owners and engineer(services, part)):
                continue
            rows[str(tag)] = {"file": _file(part), "caller": (owners.get(key) or {}).get("caller")}
        table = _table(services)
        if table.get("persisted") == rows and path.exists():
            return
        table["persisted"] = rows
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"pid": os.getpid(), "parts": rows}), encoding="utf-8")
        os.replace(temp, path)


def adopt(services) -> None:
    """Once per services (a host start or reload): the loaded parts the owners file lists
    for this NX process (same tag and file) are agent parts again, with their caller.
    Never raises."""
    table = _table(services)
    if table.get("adopted"):
        return
    table["adopted"] = True
    path = _owners_path(services)
    with contextlib.suppress(Exception):
        saved = json.loads(path.read_text(encoding="utf-8")) if path else {}
        if saved.get("pid") != os.getpid():
            return
        rows = saved.get("parts") or {}
        for part in services.parts() or []:
            row = rows.get(str(_tag(part)))
            if not row or row.get("file") != _file(part):
                continue
            if engineer(services, part):
                services.own(part)
            _owners(services).setdefault(services.part_key(part), {"caller": row.get("caller")})


# ---------------------------------------------------------------------------
# Who a part belongs to


def engineer(services, part) -> bool:
    """Whether ``part`` is the engineer's: not created or opened by an agent call (a part
    the engineer only granted is still theirs). Unknown: not the engineer's."""
    try:
        created = getattr(services, "created", None)
        return not bool(created(part) if created is not None else services.owns(part))
    except Exception:
        return False


def _granted(services, part) -> bool:
    try:
        granted = getattr(services, "granted", None)
        return bool(granted(part)) if granted is not None else False
    except Exception:
        return False


def owner(services, part) -> str | None:
    """The id of the caller that loaded ``part`` (None: unknown)."""
    with contextlib.suppress(Exception):
        return (_owners(services).get(services.part_key(part)) or {}).get("caller")
    return None


def other_agent(services, part, caller) -> str | None:
    """The owner id of ``part`` when both it and the caller's id are known and differ."""
    mine, theirs = _caller_id(caller), owner(services, part)
    return theirs if mine and theirs and mine != theirs else None


ACTIONS = {
    "close": "closes",
    "activate": "activates or shows",
    "save": "saves",
}


def refuse_engineer(services, part, action: str, *, field: str | None = "/part") -> None:
    """``NX_ENGINEER_PART`` when ``part`` is the engineer's (a granted part may be
    saved)."""
    if not engineer(services, part) or (action == "save" and _granted(services, part)):
        return
    name = targets.file_name(part)
    hint = {
        "close": f"Leave {name} to the engineer; close only parts you created or opened",
        "activate": f'Calls reach {name} without activation: read it with "part": "{name}"',
        "save": f"Ask the engineer to save {name}, or to tick 'Allow agent to change' for it "
        "in the NX MCP panel; or save your work as a part of your own",
    }[action]
    fail(
        "NX_ENGINEER_PART",
        f"{name} is the engineer's part (not created or opened by an agent): no agent "
        f"tool {ACTIONS[action]} it",
        field=field,
        hint=hint,
        details={"part": name, "action": action},
    )


def _other_agent_refusal(services, part, action: str, theirs: str, field) -> None:
    name = targets.file_name(part)
    fail(
        "NX_PART_BUSY",
        f"{name} belongs to another agent: another MCP connection ({theirs}) loaded it",
        field=field,
        hint=f"Leave {name} to that agent. If it is yours (your MCP connection restarted) "
        f"or abandoned, repeat the {action} with force: true",
        details={"part": name, "reason": "other_agent", "owner": theirs, "action": action},
    )


def refuse_activate(services, part, caller, *, force: bool, field: str = "/part") -> None:
    """Activating: never the engineer's part; another agent's only with ``force``."""
    refuse_engineer(services, part, "activate", field=field)
    theirs = other_agent(services, part, caller)
    if theirs is not None and not force:
        _other_agent_refusal(services, part, "activate", theirs, field)


def facts(services, part, caller, *, save: bool, now=None) -> dict | None:
    """Why a close of ``part`` (an agent part) is refused without ``force``, or None."""
    now = time.time() if now is None else now
    try:
        name = targets.file_name(part)
        theirs = other_agent(services, part, caller)
        if theirs is not None:
            return {"part": name, "reason": "other_agent", "owner": theirs}
        if save or not (isinstance(caller, dict) and caller.get("shared")):
            return None
        if not bool(getattr(part, "IsModified", True)):
            return None
        entry = _entries(services).get(services.part_key(part))
        changed = _unsaved_age(entry, now)
        worked = _age(entry, now)
        if changed is None or worked is None or worked > RECENT_SECONDS or entry is None:
            return None
    except Exception:
        return None
    return {
        "part": name,
        "reason": "recent_change",
        "changed_seconds_ago": round(changed, 1),
        "worked_seconds_ago": round(worked, 1),
        "last_change": dict(entry.get("change") or {}),
        "last_call": entry.get("tool"),
        "recent_seconds": RECENT_SECONDS,
    }


def refuse_close(services, part, caller, *, save: bool, force: bool, field="/part") -> None:
    """Closing: never the engineer's part; another agent's part and a busy part (shared
    connection, unsaved recent changes discarded) only with ``force``."""
    refuse_engineer(services, part, "close", field=field)
    if force:
        return
    found = facts(services, part, caller, save=save)
    if found is None:
        return
    if found["reason"] == "other_agent":
        _other_agent_refusal(services, part, "close", found["owner"], field)
    name = found["part"]
    fail(
        "NX_PART_BUSY",
        f"Closing {name} would discard its unsaved changes "
        f"({found['last_change'].get('tool')} {found['changed_seconds_ago']:.0f} s ago; a "
        f"call worked in it {found['worked_seconds_ago']:.0f} s ago). Several agents share "
        f"this MCP connection, so NX MCP cannot tell whether {name} is yours",
        field=field,
        hint=f"If {name} is yours, repeat the close with force: true (or save: true to keep "
        "the changes); otherwise leave it to the agent working in it",
        details=found,
    )


# ---------------------------------------------------------------------------
# Assembly closes


def loaded_at(services, part) -> float:
    """When ``part`` was loaded (0: before the table knew it)."""
    with contextlib.suppress(Exception):
        entry = _entries(services).get(services.part_key(part)) or {}
        return float(entry.get("loaded_at") or 0.0)
    return 0.0


def component_reason(services, part, since: float, now=None) -> str | None:
    """Why an assembly close keeps the component ``part``: a call worked in it within
    ``BUSY_SECONDS``, after ``since`` (the assembly's load). None otherwise."""
    now = time.time() if now is None else now
    try:
        entry = _entries(services).get(services.part_key(part))
    except Exception:
        return None
    age = _age(entry, now)
    if age is None or entry is None or float(entry["at"]) <= since:
        return None
    return (
        f"in use: {entry.get('tool')} worked in it {age:.0f} s ago, after the assembly "
        "was loaded; close it with nx_close_part when it is free"
    )

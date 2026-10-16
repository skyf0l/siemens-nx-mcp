"""One generic proxy for catalogue tools (architecture.md 1.6, gates 1-3, 14 and 15 in S).

``CatalogueProxy.call`` validates the arguments with the generated Pydantic
adapter (gate 1), resolves workspace paths (gate 2), applies the evidence gate
for the build in the bridge descriptor (gate 3), forwards the call with its
``operation_id`` and ``intent``, and maps every answer to the surface 1.6
envelope: a kernel failure arrives as ``details.envelope`` (F1), any other
bridge error is classified here. A failure before forwarding is ``not_started``;
after forwarding it is never relabelled ``not_started`` unless NX said so.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import itertools
import json
import math
import os
import re
import time
import typing
import uuid
import weakref
from collections import Counter
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from mcp.types import CallToolResult, ImageContent, ResourceLink, TextContent
from pydantic import AnyUrl, ValidationError

from nx_mcp.bridge import CONNECT_RETRY_SECONDS, start_deadline
from nx_mcp.core import catalogue, errors, evidence, target
from nx_mcp.core.errors import NXError
from nx_mcp.core.receipts import OPERATION_ID_HINT, new_operation_id, operation_id_problem
from nx_mcp.core.types import REF_PATTERN, RefKind
from nx_mcp.core.validate import child_problems, combine, is_union, unknown_field, unwrap
from nx_mcp.kit.snapshots import claim
from nx_mcp.sidecar import schema
from nx_mcp.workspace import WorkspaceViolation, kinds, relative, scrub, suffix_of

if TYPE_CHECKING:
    from nx_mcp.sidecar.local import SidecarServices

COMPACT_ITEMS = 20
# A compact list of more references (changes rows, a mutation's data bodies) shows only
# its first and last; the full list stays in the result store.
COMPACT_REF_ROWS = 3
# Terse changes only when they save more than the result_id/omitted/counts they add.
TERSE_MIN_SAVING = 160
# NX's internal helpers: auto-named expressions (p12) and sketch-internal geometry
# ("ENTITY 3 1 1"); kept in the stored full result, counted in compact detail.
HELPER_NAME = re.compile(r"^(p\d+|ENTITY \d+ \d+ \d+)$")
INLINE_IMAGE_BYTES = 8 * 1024 * 1024


def result(envelope: dict, *, extra: list | None = None) -> CallToolResult:
    """An MCP result carrying ``envelope`` as text and structured content (+ ``extra``)."""
    content: list = [
        TextContent(
            type="text",
            text=json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
        )
    ]
    content.extend(extra or [])
    return CallToolResult(
        content=content,
        structuredContent=envelope,
        isError=not envelope.get("ok", False),
    )


def failure(
    tool: str,
    error: NXError,
    outcome: str,
    *,
    operation_id: str | None = None,
    retry: str | None = None,
) -> dict:
    """The error envelope of ``error`` with ``mutation_outcome`` = ``outcome``.

    An ``unknown`` outcome is never retryable: NX may still finish (a ``long`` tool
    can outlast the bridge wait), so the hint names the status query.
    """
    body = error.error_dict()
    if retry:
        body["retry"] = retry
    if outcome == "unknown":
        body["retry"] = "after_status_check"
        body["hint"] = (
            "NX may still be running or have finished; query nx_get_operation with "
            f"operation_id {operation_id} before retrying"
            if operation_id
            else "NX may still be running or have finished; check the model before retrying"
        )
    envelope: dict[str, Any] = {"ok": False, "tool": tool}
    if operation_id:
        envelope["operation_id"] = operation_id
    envelope.update(mutation_outcome=outcome, error=body)
    return envelope


def from_bridge_error(tool: str, effect: str, error: NXError, operation_id=None) -> dict:
    """Map a bridge failure to an envelope; the kernel's own envelope wins (F1)."""
    details = dict(error.details or {})
    kernel = details.get("envelope")
    if isinstance(kernel, dict) and isinstance(kernel.get("error"), dict):
        return kernel
    stated = details.get("mutation_outcome")
    classified = errors.classify(error)
    classified.details.pop("envelope", None)
    classified.details.pop("mutation_outcome", None)  # stated once, at the envelope level
    operation_id = details.get("operation_id") or operation_id
    classified.details.pop("operation_id", None)
    if effect == "read":
        outcome = "not_applicable"
    elif stated in schema.OUTCOMES:
        outcome = stated
    else:
        # The request was forwarded and NX did not state an outcome.
        outcome = "unknown"
    return failure(tool, classified, outcome, operation_id=operation_id)


def _walk_paths(args: dict, pointer: str):
    """``(container, key)`` pairs that hold the value at ``pointer`` (``*`` = every item)."""
    parts = pointer.strip("/").split("/")
    frontier: list[tuple[Any, Any, Any]] = [(None, None, args)]  # (container, key, value)
    for part in parts:
        nxt: list[tuple[Any, Any, Any]] = []
        for _, _, node in frontier:
            if part == "*" and isinstance(node, list):
                nxt.extend((node, i, v) for i, v in enumerate(node))
            elif isinstance(node, dict) and part in node:
                nxt.append((node, part, node[part]))
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                nxt.append((node, int(part), node[int(part)]))
        frontier = nxt
    return [(c, k) for c, k, v in frontier if c is not None and isinstance(v, str)]


def _takes_refs(spec, pointer: str) -> bool:
    """Whether the parameter at ``pointer`` is a union with a reference form (ref|path):
    a ref-shaped value there is a ref (gate 1 matched it so), never a path."""
    param = getattr(spec, "params", {}).get(pointer.strip("/").split("/")[0])
    if param is None:
        return False
    base, _ = unwrap(param.hint)
    forms = typing.get_args(base) if is_union(base) else ()
    return any(RefKind in unwrap(form)[1] for form in forms)


def _scrubbed(value, root):
    """``value`` (an envelope) with workspace paths in its strings made relative."""
    if isinstance(value, str):
        return scrub(value, root)
    if isinstance(value, dict):
        return {k: _scrubbed(v, root) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrubbed(v, root) for v in value]
    return value


def _folder_error(field: str, shown: str) -> NXError:
    return NXError(
        "NX_WRONG_KIND",
        f"{field}: '{shown}' is a folder; give a file path",
        field=field,
        hint="Name a file inside it, e.g. '" + shown.rstrip("/") + "/name.<suffix>'; "
        "nx_list_files shows what it holds",
        details={"path": shown},
    )


def _tools_taking(name: str, suffix: str, *, reads: bool) -> list[str]:
    """Catalogue tools other than ``name`` whose path arguments read (``reads``) or write
    a ``suffix`` file: what a wrong-suffix error points to (a .step given to
    nx_open_part: nx_import_model)."""
    found = []
    for spec in catalogue.load().values():
        if spec.name == name:
            continue
        for _pointer, mode, suffixes in spec.paths:
            if suffix in suffixes and (mode == "read") == reads:
                found.append(spec.name)
                break
    # Tools that take few suffixes are the specific ones (nx_import_model before upload).
    return sorted(found, key=lambda n: (len(_suffixes_of(n)), n))


def _suffixes_of(name: str) -> set:
    spec = catalogue.get(name)
    return {s for _p, _m, suffixes in (spec.paths if spec else ()) for s in suffixes}


def _is_ref(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("id"), str) and "kind" in value


def _data_refs(value, *, top: bool) -> set:
    """IDs of the references in ``data``: those it names at its top level (a reference or a
    list of references), or (``top`` False) anywhere inside it."""
    found: set = set()
    if not isinstance(value, dict):
        return found
    if top:
        for item in value.values():
            items = item if isinstance(item, list) else [item]
            found.update(i["id"] for i in items if _is_ref(i))
        return found

    def walk(node):
        if _is_ref(node):
            found.add(node["id"])
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(value)
    return found


def _ends(rows: list) -> list:
    """``rows`` when there are at most COMPACT_REF_ROWS, else the first and the last."""
    return rows if len(rows) <= COMPACT_REF_ROWS else [rows[0], rows[-1]]


def _cut_note(total: int, kept: int, first_last: bool = True) -> dict:
    note: dict[str, Any] = {"total": total, "returned": kept}
    if first_last and total > COMPACT_REF_ROWS and kept == 2:
        note["shown"] = "first_last"
    return note


def _terse_changes(envelope: dict, omitted: dict) -> dict | None:
    """Compact ``changes``. Created and modified keep their rows even when ``data`` shows
    the objects (an agent reads ``changes`` alone to learn what a call changed): when
    ``data`` names the call's main objects at its top level (its feature, body or sketch),
    those rows, else the rows that are not NX helpers. Deleted drops the rows ``data``
    shows (no loss) and keeps the others (invalid references). A kept list of more than
    COMPACT_REF_ROWS shows its first and last row. ``counts`` gives every row of a cut list
    by kind, and the full rows stay in the result store (``nx_get_result``)."""
    changes = envelope.get("changes")
    if not isinstance(changes, dict):
        return None
    data = envelope.get("data")
    top, shown = _data_refs(data, top=True), _data_refs(data, top=False)
    lossless, terse, counts = dict(changes), dict(changes), {}
    notes = {}
    for key in ("created", "modified", "deleted"):
        rows = changes.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        if key == "deleted":
            unseen = [r for r in rows if not (_is_ref(r) and r["id"] in shown)]
            lossless[key] = terse[key] = candidates = unseen
        else:
            unseen = rows
            candidates = [r for r in rows if _is_ref(r) and r["id"] in top]
            if not candidates:
                candidates = [r for r in rows if not HELPER_NAME.match(str(r.get("name") or ""))]
        keep = _ends(candidates)
        if len(keep) < len(unseen):
            kinds = Counter(str(r.get("kind")) for r in rows if isinstance(r, dict))
            counts[key] = dict(sorted(kinds.items()))
            ends = len(candidates) > COMPACT_REF_ROWS
            notes[f"/changes/{key}"] = _cut_note(len(rows), len(keep), ends)
            terse[key] = keep
    if counts:
        terse["counts"] = counts
        if _size(lossless) - _size(terse) >= TERSE_MIN_SAVING:
            omitted.update(notes)
            return terse
    # A few rows cost less than the result_id, omitted and counts that would replace them.
    return None if lossless == changes else lossless


def _terse_data(envelope: dict, omitted: dict) -> dict | None:
    """Compact ``data`` of a committed mutation: a top-level list of more than
    COMPACT_REF_ROWS body references (a pattern's instance bodies) shows its first and last
    row; the tool lists its seed or target body first."""
    data = envelope.get("data")
    if envelope.get("mutation_outcome") != "committed" or not isinstance(data, dict):
        return None
    out = None
    for key, value in data.items():
        if (
            isinstance(value, list)
            and len(value) > COMPACT_REF_ROWS
            and all(_is_ref(v) and v.get("kind") == "body" for v in value)
        ):
            out = dict(data) if out is None else out
            out[key] = _ends(value)
            omitted[f"/data/{key}"] = _cut_note(len(value), 2)
    return out


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _project(envelope: dict, store) -> dict:
    """Compact detail (gate 14, S): terse ``data`` body lists (:func:`_terse_data`) and
    ``changes`` (:func:`_terse_changes`), arrays over 20 items cut; the full result goes to
    the result store for ``nx_get_result``."""
    if not envelope.get("ok"):
        return envelope
    omitted: dict[str, Any] = {}
    paged = envelope.get("page") is not None
    terse = _terse_changes(envelope, omitted)
    terse_data = _terse_data(envelope, omitted)

    def cut(value, path):
        if isinstance(value, dict):
            return {k: cut(v, f"{path}/{k}") for k, v in value.items()}
        if isinstance(value, list):
            if len(value) > COMPACT_ITEMS and not (paged and path == "/data/items"):
                omitted[path] = {"total": len(value), "returned": COMPACT_ITEMS}
                value = value[:COMPACT_ITEMS]
            return [cut(v, f"{path}/{i}") for i, v in enumerate(value)]
        if isinstance(value, float) and math.isfinite(value):
            return float(f"{value:.12g}")  # 250.00000000000003 -> 250.0 (full: nx_get_result)
        return value

    envelope_view = {**envelope, "changes": terse} if terse is not None else envelope
    if terse_data is not None:
        envelope_view = {**envelope_view, "data": terse_data}
    projected = {
        k: v if k in ("warnings", "error", "page") else cut(v, "/" + k)
        for k, v in envelope_view.items()
    }
    if not omitted:
        return projected
    try:
        result_id = store.put(envelope)
    except Exception:
        return envelope  # A storage fault never changes a finished call's answer.
    projected.update(result_id=result_id, omitted=omitted, expand_with="nx_get_result")
    return projected


# Caller identity (core/busy.py): every NX call carries ``caller`` = {"id", "shared"}.
# ``id`` is this sidecar process, its MCP session and an id the client puts in the request's
# ``_meta`` (one of CLIENT_ID_KEYS, any namespace prefix); ``shared`` is true while calls of
# that id overlapped within SHARED_SECONDS (several agents behind one connection).
PROCESS_ID = "sc-" + uuid.uuid4().hex[:8]
CALLER: contextvars.ContextVar[str | None] = contextvars.ContextVar("nx_caller", default=None)
CLIENT_ID_KEYS = frozenset({"agentid", "sessionid", "clientsessionid"})
SHARED_SECONDS = 120.0
_SESSIONS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # session -> number
_NUMBERS = itertools.count(1)  # never reused, also after a session is gone


def caller_id(context) -> str:
    """The caller id of an MCP request context (``request_context`` of the server)."""
    ident = PROCESS_ID
    session = getattr(context, "session", None)
    if session is not None:
        try:
            number = _SESSIONS.get(session)
            if number is None:
                number = _SESSIONS[session] = next(_NUMBERS)
            ident += f".{number}"
        except TypeError:  # a session object without weak references
            ident += ".1"
    client = client_id(getattr(context, "meta", None))
    return f"{ident}:{client}" if client else ident


def client_id(meta) -> str | None:
    """An agent or session id the MCP client put in the request's ``_meta``, else None."""
    if meta is None:
        return None
    try:
        values = meta.model_dump() if hasattr(meta, "model_dump") else dict(meta)
    except Exception:
        return None
    for key, value in values.items():
        name = re.sub(r"[^a-z]", "", str(key).rsplit("/", 1)[-1].lower())
        if name in CLIENT_ID_KEYS and isinstance(value, (str, int)) and str(value).strip():
            return re.sub(r"[^A-Za-z0-9_.:-]", "", str(value))[:64] or None
    return None


# Bridge failures the sidecar explains by looking at NX from outside (explain_stall).
STALL_CODES = ("NX_BRIDGE_UNAVAILABLE", "NX_OUTCOME_UNKNOWN", "NX_BUSY", "NX_MODAL_DIALOG_OPEN")
STATUS_TTL = 30.0  # seconds a listing trusts the session state read from nx_get_status
STATUS_TIMEOUT = 5.0
# nx_get_status waits this long for NX, then the sidecar answers from outside NX
# (host/heartbeat.py). It does not wait at all once NX's window has stopped responding
# during one call that has run this long: NX accepts no connection until that call ends.
STATUS_WAIT = 6.0
STATUS_HELD_SECONDS = 5.0
# Another call is not sent (NX_BUSY, nothing ran) while NX's window has stopped
# responding during one call that has run this long: it could not start before that
# call ends, and a changing call expires after the bridge's queue time anyway.
HELD_REFUSE_SECONDS = 120.0


class CatalogueProxy:
    def __init__(self, bridge, workspace, store, *, build=None) -> None:
        self.bridge = bridge
        self.workspace = workspace
        self.store = store
        self._build = build
        self._services: SidecarServices | None = None  # sidecar/local.py, made on first use
        # Session state of the connected NX, from nx_get_status: the license failures
        # and the tools found unsupported on its build that it remembers, and why it lets
        # agents only read (an untested build). ``_key`` is the bridge descriptor it was
        # read for.
        self._key: tuple | None = None
        self._read_at = 0.0
        self.licenses: list[dict] = []
        self.versions: list[dict] = []
        self.read_only_reason: str | None = None
        self._running: dict[str, int] = {}  # caller id -> its calls waiting for NX now
        self._overlap: dict[str, float] = {}  # caller id -> when two of its calls overlapped

    def _descriptor(self):
        path = getattr(self.bridge, "descriptor_path", None)
        if not isinstance(path, (str, os.PathLike)):
            return None
        try:
            from nx_mcp.bridge import BridgeDescriptor

            return BridgeDescriptor.read(path)
        except (OSError, ValueError, TypeError):
            return None

    # The build of the connected NX, from the bridge descriptor (None when unknown).
    def build(self) -> str | None:
        if self._build is not None:
            return self._build() if callable(self._build) else self._build
        descriptor = self._descriptor()
        return evidence.build_key(descriptor.nx_version) if descriptor else None

    def nx_build(self) -> str | None:
        """NX's full build text from the bridge descriptor (``2406.9160``), else the key."""
        descriptor = self._descriptor()
        if descriptor is not None and descriptor.nx_version:
            return str(descriptor.nx_version)
        return self.listing()[0]

    def build_tested(self) -> bool:
        """Whether the listing build is tested (an assumed default build is)."""
        build, assumed = self.listing()
        return assumed or evidence.known_build(build)

    def read_only(self) -> bool:
        """Only read and view tools on this untested build (``evidence.restricted``)."""
        return evidence.restricted(self.listing()[0], bool(self.read_only_reason))

    def listing(self) -> tuple[str | None, bool]:
        """``(build, assumed)`` that listings follow: the connected build, or the default
        supported build while no NX is connected (no bridge descriptor)."""
        build = self.build()
        if build is None and self._build is None and self._descriptor() is None:
            return evidence.DEFAULT_BUILD, True
        return build, False

    def connected_key(self) -> tuple | None:
        descriptor = self._descriptor()
        if descriptor is None:
            return None
        return (descriptor.pid, descriptor.port, descriptor.token, descriptor.nx_version)

    def nx_health(self, *, reader: str | None = None, sample: float | None = None):
        """``(health, heartbeat)``: NX's state seen from outside NX (host/heartbeat.py),
        in about a second and without a bridge call; never raises."""
        from nx_mcp.host import heartbeat

        root = getattr(self.workspace, "root", None)
        descriptor = self._descriptor()
        pid = getattr(descriptor, "pid", None)
        try:
            health = heartbeat.assess(
                root,
                pid,
                registered=descriptor is not None,
                reader=reader,
                sample=heartbeat.SAMPLE_SECONDS if sample is None else sample,
            )
        except Exception as error:  # a diagnostic never fails the call
            health = {"state": "unknown", "pid": pid, "process": {}, "running": None,
                      "heartbeat_age_seconds": None, "dialogs": [],
                      "message": f"NX's state could not be read: {error}",
                      "hint": "Retry nx_get_status in a few seconds"}  # fmt: skip
        beat = heartbeat.heartbeat_for(root, pid) if root is not None else None
        return health, beat

    def held(self, seconds: float) -> dict | None:
        """The agent call that has held NX's main thread for at least ``seconds`` while
        NX's window does not respond (NX accepts no connection then), else None. Reads
        the heartbeat file first: no process check unless NX runs a call that long."""
        from nx_mcp.host import dialogs, heartbeat

        root = getattr(self.workspace, "root", None)
        descriptor = self._descriptor()
        pid = getattr(descriptor, "pid", None)
        if root is None or descriptor is None:
            return None
        try:
            call = heartbeat.running_call(root, pid)
            if not call or (call.get("seconds") or 0) < seconds:
                return None
            if dialogs.process_alive(pid) is not True or heartbeat.responding(pid) is not False:
                return None
        except Exception:
            return None
        return call

    def _held_status(self, args: dict) -> dict | None:
        """nx_get_status answered at once while one call holds NX (``held``), else None."""
        if self.held(STATUS_HELD_SECONDS) is None:
            return None
        from nx_mcp.domains.session import status

        health, beat = self.nx_health(reader=args.get("operation_id"))
        if health.get("state") not in ("busy", "unresponsive", "blocked_by_dialog"):
            return None
        return status.sidecar_envelope(
            self.workspace.root, health, args, build=self.build(), beat=beat
        )

    def _refuse_held(self, spec, operation_id) -> dict | None:
        """NX_BUSY (nothing sent) while one call has held NX's main thread for
        HELD_REFUSE_SECONDS, else None."""
        if self.held(HELD_REFUSE_SECONDS) is None:
            return None
        health, _ = self.nx_health(sample=0.5)
        call = health.get("running")
        if health.get("state") != "busy" or not call:
            return None
        error = NXError(
            "NX_BUSY",
            f"{health['message']}. This call was not sent: it could not start before that "
            "step ends. Nothing ran.",
            hint=f"Wait; retry when nx_get_status no longer shows {call['tool']} running "
            "(it answers at once meanwhile). Do not resend heavy reads",
            retryable=True,
            details={
                "running": {k: call.get(k) for k in ("tool", "part", "seconds")},
                "nx": {k: health.get(k) for k in ("state", "pid", "process")},
            },
        )
        return failure(spec.name, error, "not_started", operation_id=operation_id)

    async def refresh(self, *, force: bool = False) -> None:
        """Read the session state from nx_get_status when NX (re)connected, when the
        last read is older than STATUS_TTL, or when ``force``; never raises."""
        key = self.connected_key()
        if key is None:
            self._key, self.licenses, self.versions, self.read_only_reason = None, [], [], None
            return
        fresh = time.monotonic() - self._read_at < STATUS_TTL
        if key == self._key and fresh and not force:
            return
        if key != self._key:  # a new NX session remembers nothing yet
            self.licenses, self.versions, self.read_only_reason = [], [], None
        self._key, self._read_at = key, time.monotonic()
        if await asyncio.to_thread(self.held, STATUS_HELD_SECONDS):
            return  # NX accepts no connection now: keep what is known
        try:
            answer = await asyncio.wait_for(
                self.bridge.call("nx_get_status", {"include": ["bridge"]}), STATUS_TIMEOUT
            )
            bridge = ((answer or {}).get("data") or {}).get("bridge") or {}
            rows = bridge.get("license_unavailable")
        except Exception:
            return  # NX busy or down: keep what is known
        if isinstance(rows, list):
            self.licenses = [r for r in rows if isinstance(r, dict) and r.get("tool")]
            failed = bridge.get("version_unsupported") or []
            self.versions = [r for r in failed if isinstance(r, dict) and r.get("tool")]
            reason = bridge.get("read_only")
            self.read_only_reason = str(reason) if reason else None

    def explain_stall(self, error: NXError) -> NXError:
        """Say truthfully why NX did not answer: a modal NX dialog blocks it (seen from
        here, outside NX: NX's own report may never arrive), NX is not running, or NX
        runs but its bridge does not accept connections. Other errors pass unchanged;
        never raises (a diagnostic must not fail the call)."""
        if error.code not in STALL_CODES:
            return error
        try:
            return self._explain_stall(error)
        except Exception:
            return error

    def _explain_stall(self, error: NXError) -> NXError:
        from nx_mcp.host import dialogs

        descriptor = self._descriptor()
        pid = getattr(descriptor, "pid", None)
        alive = dialogs.process_alive(pid)
        if not pid or alive is None:
            return error
        details = dict(error.details or {})
        if not alive:
            if error.code != "NX_BRIDGE_UNAVAILABLE":
                return error
            return NXError(
                error.code,
                f"NX (pid {pid}) is not running: {error.message}",
                hint="Start NX with the NX MCP host, or ask the engineer; "
                "check nx_get_operation before retrying a mutation",
                retryable=error.retryable,
                details=details,
            )
        found = dialogs.scan(pid, host_disabled=dialogs.ANY_OWNER)
        if found:
            if error.code == "NX_MODAL_DIALOG_OPEN":
                return error
            outcome = details.get("mutation_outcome") or "unknown"
            first = found[0]
            message = dialogs.blocked_message(
                {"title": first.title, "text": first.text}, outcome=outcome
            )
            then = (
                "then query nx_get_operation before retrying"
                if outcome != "not_started"
                else "nx_get_status shows when NX is free; then retry"
            )
            return NXError(
                "NX_MODAL_DIALOG_OPEN",
                message,
                hint=dialogs.hint_for(
                    first.title,
                    "Ask the engineer to answer the NX dialog (the agent never clicks it)",
                )
                + "; "
                + then,
                details={**details, "dialogs": [d.row() for d in found], "seen_from": "sidecar"},
            )
        busy = self._explain_busy(error, details) if error.code != "NX_MODAL_DIALOG_OPEN" else None
        if busy is not None:
            return busy
        if error.code == "NX_BRIDGE_UNAVAILABLE":
            from nx_mcp.host import heartbeat

            root = getattr(self.workspace, "root", None)
            reload = heartbeat.reload_state(root)
            if reload is not None:
                why = "its NX MCP host is reloading (development reload"
                why += f", generation {reload['generation']})" if reload.get("generation") else ")"
                hint = "Retry in a few seconds; nx_get_status shows when the host answers"
            elif (
                root is not None
                and heartbeat.read(root) is not None
                and not heartbeat.fresh(root, pid)
            ):
                why = "its NX MCP host is stopped or silent (no fresh heartbeat)"
                hint = "Ask the engineer whether the NX MCP host runs; never kill NX yourself"
            else:
                why = "its NX MCP host did not accept the connection (busy)"
                hint = "Retry in a few seconds; nx_get_status shows what NX runs"
            details["nx"] = {"pid": pid, "reloading": reload is not None}
            return NXError(
                error.code,
                f"{error.message} NX (pid {pid}) is running, but {why}.",
                hint=hint,
                retryable=error.retryable,
                details=details,
            )
        return error

    def _explain_busy(self, error: NXError, details: dict) -> NXError | None:
        """The stall told by NX's heartbeat: the call NX runs (busy) or the host's silence
        (unresponsive); None when the heartbeat explains nothing (no heartbeat, the host
        idle, or a modal dialog, which ``_explain_stall`` reports)."""
        from nx_mcp.host import heartbeat

        root = getattr(self.workspace, "root", None)
        if root is None or heartbeat.read(root) is None:
            return None  # no heartbeat (batch host): nothing to add
        health, _ = self.nx_health(sample=0.5)
        state = health.get("state")
        if state not in ("busy", "unresponsive"):
            return None
        outcome = details.get("mutation_outcome") or "unknown"
        then = (
            "query nx_get_operation with the operation_id before retrying"
            if outcome != "not_started"
            else "retry when nx_get_status shows NX free"
        )
        return NXError(
            error.code,
            f"{error.message} {health['message']}.",
            hint=f"{health['hint']}; then {then}",
            retryable=error.retryable,
            retry=error.retry,
            details={
                **details,
                "nx": {k: health.get(k) for k in ("state", "pid", "process", "running")},
            },
        )

    def signature(self) -> tuple:
        """What listings depend on; a change sends tools/list_changed."""
        rows = frozenset((r.get("tool"), r.get("variant")) for r in self.licenses)
        failed = frozenset((r.get("tool"), r.get("variant")) for r in self.versions)
        return (*self.listing(), self.dev(), rows, failed, self.read_only_reason)

    @staticmethod
    def dev() -> bool:
        return os.environ.get("NX_MCP_DEV") == "1"

    def remembered(self, spec, variant: str | None = None) -> dict | None:
        """The license failure NX remembers for this tool (``variant``: or that variant)."""
        return evidence.remembered(self.licenses, spec.name, variant)

    def failed(self, spec, variant: str | None = None) -> dict | None:
        """The tool (``variant``: or that variant) NX found unsupported on its build this
        session (a missing NXOpen member); development mode ignores it."""
        if self.dev():
            return None
        return evidence.remembered(self.versions, spec.name, variant)

    def variants(self, spec) -> tuple[str, ...]:
        """The variant values listed for ``spec``: offered on the listing build (tested
        there, or tested on another build: ``evidence.offered``), not refused for a license
        nor found unsupported; development mode lists every value."""
        values = spec.variant_values()
        if not values:
            return values
        build, _ = self.listing()
        usable = (
            values if self.dev() else evidence.variants_for(spec, build, read_only=self.read_only())
        )
        if not isinstance(spec.variants, str):
            return usable
        return tuple(v for v in usable if not self.remembered(spec, v) and not self.failed(spec, v))

    def unavailable(self, spec, variant: str | None = None) -> str | None:
        """Why ``spec`` (or its ``variant``) is not offered on this build, or None.
        Development mode offers untested tools and variants (marked tested: false)."""
        own = variant if isinstance(spec.variants, str) else None
        entry = self.remembered(spec, own)
        if entry is not None:
            return f"no NX license on this seat (remembered from {entry.get('since')})"
        failed = self.failed(spec, own)
        if failed is not None:
            return f"failed on {evidence.build_label(failed.get('build') or self.nx_build())}"
        if self.dev():
            return None
        build, assumed = self.listing()
        where = f"NX build {build}" + (" (assumed: NX not connected)" if assumed else "")
        read_only = self.read_only()
        if not evidence.visible(spec, build, read_only=read_only):
            if evidence.tested_on(spec) and read_only:
                return f"read-only on {where} (not tested with NX MCP)"
            return "not tested on any NX build"
        if variant is not None and variant not in evidence.variants_for(
            spec, build, read_only=read_only
        ):
            return f"variant {variant!r} is not tested on any NX build"
        if spec.variant_values() and not self.variants(spec):
            return f"no variant is available on {where}"
        return None

    def visible(self, spec) -> bool:
        """Discovery filter (decision 11); development mode shows untested tools."""
        return self.unavailable(spec) is None

    def tested(self, spec) -> bool:
        return evidence.available(spec, self.listing()[0])

    def _offered(self, spec) -> tuple | None:
        try:
            return self.variants(spec) if isinstance(spec.variants, str) else None
        except Exception:
            return None  # no build known yet: errors list every variant

    def _invalid(self, spec, args) -> NXError | None:
        """Gate 1. ``args`` becomes its canonical form (natural spellings resolved, as
        ``core.validate`` gives the handler), so the evidence gate and NX see the variant
        and values that run."""
        unknown = sorted(k for k in args if k not in spec.params)
        if unknown:
            # Unknown fields offer the call keys too ('part' when 2+ parts are loaded):
            # "takes no arguments" would be false for a tool that takes ``part``.
            fields = [*spec.params, *schema.call_fields(spec)]
            found = [unknown_field([], name, fields) for name in unknown]
            try:
                spec.check({k: v for k, v in args.items() if k in spec.params},
                           shown=self._offered(spec))  # fmt: skip
            except NXError as precise:
                found.append(precise)
            return combine(found)
        try:
            # The precise messages, every invalid field at once; a variant error lists
            # only the variants this build offers.
            canonical = spec.check(args, shown=self._offered(spec)).args
        except NXError as precise:
            return precise
        try:
            schema.adapter(spec).validate_python(args)  # same rules (parity test)
        except ValidationError as error:
            found = []
            for row in error.errors():
                field = schema.error_pointer(args, row["loc"], row["type"]) or None
                found.append(
                    NXError(
                        "NX_INVALID_ARGUMENT", f"{field or 'arguments'}: {row['msg']}", field=field
                    )
                )
            return combine(found)
        # The steps of a batch or preview: each against its own tool's schema, so a
        # malformed step fails here at once instead of after its wait in NX's queue.
        children = child_problems(spec.params, canonical, catalogue.get, target.split_call_keys)
        if children is not None:
            return children
        args.clear()
        args.update(canonical)
        return None

    def _resolve_paths(self, spec, args: dict) -> NXError | None:
        """Gate 2 (sidecar side): each path argument resolved inside the workspace, after
        the suffix and folder rules of :meth:`_path_problem`."""
        for pointer, mode, suffixes in spec.paths:
            refs = _takes_refs(spec, pointer)
            for container, key in _walk_paths(args, pointer):
                value = container[key]
                if refs and re.match(REF_PATTERN, value):
                    continue  # 'p:3' in a ref|path union names an object, not a file
                field = pointer.replace("*", str(key)) if "*" in pointer else pointer
                try:
                    resolved = self.workspace.resolve(value)
                except WorkspaceViolation as error:
                    reserved = ".nx-mcp" in str(value).replace("\\", "/").casefold().split("/")
                    return NXError(
                        "NX_PATH_RESERVED" if reserved else "NX_PATH_OUTSIDE_WORKSPACE",
                        f"{field}: {error}",
                        field=field,
                        details={"path": value},
                    )
                checked = self._path_problem(spec, args, field, value, resolved, mode, suffixes)
                if isinstance(checked, NXError):
                    return checked
                container[key] = str(checked)
        return None

    def _path_problem(self, spec, args, field, given, resolved, mode, suffixes):
        """The path to send (a suffix added) or the NXError of gate 2's suffix and folder
        rules; every refusal is fixable (``retry: after_fix``):

        - a folder (an existing one, or a path ending in ``/``) where a file is wanted;
        - no suffix: a ``read`` path takes the one existing file of an allowed suffix;
          a new file takes the only allowed kind, or the one its format option names
          (``format: 'iges'`` -> ``.iges``); with several kinds and no format it is
          refused (never a text file silently named ``.prt``);
        - a suffix the tool does not take: the hint names the tools that do."""
        root = self.workspace.root
        shown = relative(root, resolved)
        text = str(given)
        if mode == "dir" or not suffixes:
            if mode != "dir" and text.endswith(("/", "\\")):
                return _folder_error(field, shown)
            return resolved
        if text.endswith(("/", "\\")) or resolved.is_dir():
            return _folder_error(field, shown)
        expected = "/".join(suffixes)
        if not resolved.suffix:
            groups = kinds(suffixes)
            if mode == "read":
                found = [resolved.with_name(resolved.name + s) for s in suffixes]
                found = [f for f in found if f.is_file()]
                if len(found) == 1 or len(groups) == 1:
                    return found[0] if found else resolved.with_name(resolved.name + suffixes[0])
                return NXError(
                    "NX_INVALID_ARGUMENT",
                    f"{field}: '{shown}' has no file suffix; name the file with its suffix "
                    f"({expected})",
                    field=field,
                    hint="Check the file name with nx_list_files on its folder",
                    details={"path": shown, "expected": list(suffixes)},
                )
            if len(groups) == 1:
                return resolved.with_name(resolved.name + suffixes[0])
            named = suffix_of(spec.variant_of(args), suffixes)
            if named is not None:
                return resolved.with_name(resolved.name + named)
            return NXError(
                "NX_INVALID_ARGUMENT",
                f"{field}: '{shown}' needs a file suffix: one of {expected}",
                field=field,
                hint=f"End the path with the suffix of the file you mean, e.g. "
                f"'{shown}{suffixes[0]}'",
                details={"path": shown, "expected": list(suffixes)},
            )
        if resolved.suffix.lower() in suffixes:
            return resolved
        suffix = resolved.suffix.lower()
        others = _tools_taking(spec.name, suffix, reads=mode == "read")
        if others:
            hint = f"A {suffix} file is {'read' if mode == 'read' else 'written'} by " + (
                " or ".join(others[:2])
            )
        elif mode == "read":
            hint = f"Name an existing {expected} file; nx_list_files shows the folder"
        elif len(kinds(suffixes)) == 1:
            hint = f"End the path with {suffixes[0]}, or leave the suffix out"
        else:
            hint = f"End the path with one of {expected}"
        return NXError(
            "NX_UNSUPPORTED_FILE_TYPE",
            f"{field}: {spec.name} takes a {expected} file, not {suffix} ('{shown}')",
            field=field,
            hint=hint,
            retry="after_fix",
            details={"path": shown, "expected": list(suffixes)},
        )

    async def call(
        self, spec, arguments: dict | None, *, intent: str | None = None, detail: str = "compact"
    ) -> CallToolResult:
        # The target keys (part, expires_at) go to NX untouched: the kernel
        # validates and resolves them (core/target.py, gate 3t). Direct core tools take
        # them too: their listed schemas leave them out, but the server does not
        # validate input against them (install: validate_input=False).
        args, keys = target.split_call_keys(spec, dict(arguments or {}))
        operation_id = args.pop("operation_id", None)
        intent = args.pop("intent", None) or intent
        detail = args.pop("detail", None) or detail
        problem = None
        if operation_id is not None and operation_id_problem(operation_id) is not None:
            problem = NXError(
                "NX_INVALID_ARGUMENT",
                str(operation_id_problem(operation_id)),
                field="/operation_id",
                hint=OPERATION_ID_HINT,
            )
        elif intent is not None and (not isinstance(intent, str) or len(intent) > 120):
            problem = NXError("NX_INVALID_ARGUMENT", "intent: at most 120 characters")
        elif detail not in ("compact", "full"):
            problem = NXError("NX_INVALID_ARGUMENT", "detail: compact or full", field="/detail")
        problem = problem or self._invalid(spec, args) or self._resolve_paths(spec, args)  # 1, 2
        if problem is None and not self.dev():  # 3
            build = self.build()
            if build is not None:
                problem = evidence.check(spec, build, args, read_only=self.read_only())
        if problem is None:  # 3: what NX remembers failing this session
            entry = self.remembered(spec, spec.variant_of(args))
            if entry is not None:
                problem = evidence.license_error(entry)
            failed = self.failed(spec, spec.variant_of(args))
            if entry is None and failed is not None:
                problem = evidence.version_error(failed)
        if problem is not None:
            return result(failure(spec.name, problem, "not_started", operation_id=operation_id))
        if "detail" in spec.params:  # nx_get_result: the call's detail is its own argument
            args["detail"] = detail
        if operation_id is None and spec.effect != "read":
            # Known before sending, so an unknown outcome can always be queried.
            operation_id = new_operation_id()
        params = dict(args)
        if spec.runs == "sidecar":  # Workspace tools never reach NX (sidecar/local.py).
            envelope = await asyncio.to_thread(self._local, spec, params, operation_id)
            envelope = self._relative_paths(spec, envelope)
            if detail == "compact":
                envelope = _project(envelope, self.store)
            return result(envelope, extra=self._attachments(envelope))
        status_call = spec.name == "nx_get_status"
        if status_call:
            held = await asyncio.to_thread(self._held_status, args)
            if held is not None:
                if detail == "compact":
                    held = _project(held, self.store)
                return result(held)
        else:
            refused = await asyncio.to_thread(self._refuse_held, spec, operation_id)
            if refused is not None:
                return result(refused)
        params.update(keys)
        if spec.effect != "read" and "expires_at" not in params:
            # The ghost guard (A27): a changing call NX cannot start within the bridge's
            # queue time of being sent never runs (bridge.start_deadline).
            params["expires_at"] = round(start_deadline(), 3)
        if operation_id:
            params["operation_id"] = operation_id
        if intent:
            params["intent"] = " ".join(intent.split())
        try:
            if status_call:  # answers within seconds: NX's own answer or the sidecar's
                try:
                    answer = await asyncio.wait_for(self._send(spec.name, params), STATUS_WAIT)
                except asyncio.TimeoutError as late:
                    raise NXError(
                        "NX_BRIDGE_UNAVAILABLE",
                        f"NX did not answer within {STATUS_WAIT:.0f} s",
                        details={"mutation_outcome": "not_started"},
                    ) from late
            else:
                answer = await self._send(spec.name, params)
        except NXError as error:
            if status_call and (error.category == "bridge" or error.code == "NX_OUTCOME_UNKNOWN"):
                # Status answers while NX is busy, hung or down: seen from outside NX.
                from nx_mcp.domains.session import status

                health, beat = await asyncio.to_thread(
                    self.nx_health, reader=args.get("operation_id")
                )
                envelope = status.sidecar_envelope(
                    self.workspace.root, health, args, build=self.build(), beat=beat,
                    error=error.as_dict(),
                )  # fmt: skip
            else:
                error = await asyncio.to_thread(self.explain_stall, error)
                envelope = from_bridge_error(spec.name, spec.effect, error, operation_id)
        except Exception as error:  # A sidecar fault after the request left.
            fault = NXError(
                "NX_ERROR",
                f"Sidecar failed handling {spec.name}: {type(error).__name__}: {error}",
            )
            envelope = failure(
                spec.name,
                fault,
                "not_applicable" if spec.effect == "read" else "unknown",
                operation_id=operation_id,
            )
        else:
            if isinstance(answer, dict) and "ok" in answer:
                envelope = answer
                full = answer.get("full_result")  # a bridge overflow snapshot: the caller's
                if isinstance(full, dict) and isinstance(full.get("id"), str):
                    claim(full["id"])
            else:
                fault = NXError(
                    "NX_PROTOCOL_MISMATCH", f"NX returned no result envelope for {spec.name}"
                )
                envelope = failure(
                    spec.name,
                    fault,
                    "not_applicable" if spec.effect == "read" else "unknown",
                    operation_id=operation_id,
                )
        envelope = self._relative_paths(spec, envelope)
        if detail == "compact":
            envelope = _project(envelope, self.store)
        return result(envelope, extra=self._attachments(envelope))

    def _relative_paths(self, spec, envelope: dict) -> dict:
        """The envelope with every absolute path inside the workspace written relative to
        it (``workspace.scrub``: results and error messages never show the host's
        folders). nx_get_status keeps the workspace root it reports."""
        root = getattr(self.workspace, "root", None)
        if root is None or spec.name == "nx_get_status":
            return envelope
        try:
            return _scrubbed(envelope, root)
        except Exception:
            return envelope  # a courtesy: never fails the finished call

    async def _send(self, method: str, params: dict) -> dict:
        """``bridge.call`` with the ``caller`` key (always the sidecar's: an agent's own
        ``caller`` argument is replaced) and the bookkeeping of overlapping calls."""
        ident = CALLER.get() or PROCESS_ID
        now = time.monotonic()
        running = self._running.get(ident, 0)
        if running:
            self._overlap[ident] = now
        shared = now - self._overlap.get(ident, -math.inf) <= SHARED_SECONDS
        params["caller"] = {"id": ident, "shared": shared}
        self._running[ident] = running + 1
        try:
            return await self._call_retrying(method, params)
        finally:
            self._running[ident] -= 1

    async def _call_retrying(self, method: str, params: dict) -> dict:
        """``bridge.call``, retried for up to CONNECT_RETRY_SECONDS (short backoff) when
        the connect itself was refused or reset (no byte written, so nothing can have
        run) while NX's host is alive by its heartbeat or reloading: its listen backlog
        was full or it was restarting. Any other failure, and one after the request
        was written, is never retried here."""
        deadline = time.monotonic() + CONNECT_RETRY_SECONDS
        delay = 0.1
        while True:
            try:
                return await self.bridge.call(method, params)
            except NXError as error:
                refused = (error.details or {}).get("connect") == "refused"
                if (
                    not refused
                    or time.monotonic() + delay > deadline
                    or not await asyncio.to_thread(self._host_alive)
                ):
                    raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1.0)

    def _host_alive(self) -> bool:
        """Whether NX's host will accept connections soon: a fresh heartbeat (or one
        saying a call runs) of the registered NX, or a development reload under way."""
        from nx_mcp.host import heartbeat

        root = getattr(self.workspace, "root", None)
        if root is None:
            return False
        try:
            pid = getattr(self._descriptor(), "pid", None)
            return heartbeat.fresh(root, pid) or heartbeat.reload_state(root) is not None
        except Exception:
            return False

    def _local(self, spec, params: dict, operation_id) -> dict:
        from nx_mcp.sidecar import local

        if self._services is None:
            self._services = local.SidecarServices(self.workspace)
        self._services.build = self.build()
        self._services.dev = self.dev()
        return local.run(spec, params, self._services, operation_id=operation_id)

    def _attachments(self, envelope: dict) -> list:
        """An inline PNG (``data.image``) and a resource link to a workspace file the
        result names (``data.path`` with ``data.sha256``), so clients fetch bytes
        through ``nx-artifact://`` instead of the conversation."""
        out: list[ImageContent | ResourceLink] = []
        image = self._image(envelope)
        if image is not None:
            out.append(image)
        data = envelope.get("data") if envelope.get("ok") else None
        if isinstance(data, dict) and isinstance(data.get("path"), str) and data.get("sha256"):
            try:
                file = self.workspace.resolve(data["path"])
                if file.is_file() and file.stat().st_size <= INLINE_IMAGE_BYTES:
                    relative = file.relative_to(self.workspace.root).as_posix()
                    out.append(
                        ResourceLink(
                            type="resource_link",
                            name=file.name,
                            uri=AnyUrl("nx-artifact://workspace/" + quote(relative, safe="")),
                            size=file.stat().st_size,
                        )
                    )
            except Exception:
                pass  # A link is a convenience; the finished call's answer stands.
        return out

    def _image(self, envelope: dict) -> ImageContent | None:
        """Inline PNG for ``data.image = {path, sha256, inline: true}`` (any tool).

        The bytes go to the client as MCP image content, never as text. A file that
        changed since the tool reported it, or one over 8 MiB, is not attached and
        the envelope gets a warning instead.
        """
        data = envelope.get("data") if envelope.get("ok") else None
        image = data.get("image") if isinstance(data, dict) else None
        if not isinstance(image, dict) or not image.get("inline"):
            return None
        try:
            file = self.workspace.resolve(str(image["path"]))
            if file.stat().st_size > INLINE_IMAGE_BYTES:
                raise ValueError("the image exceeds the 8 MiB inline limit")
            raw = file.read_bytes()
            if hashlib.sha256(raw).hexdigest() != image.get("sha256"):
                raise ValueError("the image file changed after the call reported it")
        except Exception as error:
            envelope.setdefault("warnings", []).append(
                {"code": "NX_UPDATE_WARNING", "message": f"Image not attached: {error}"}
            )
            return None
        return ImageContent(type="image", mimeType="image/png", data=base64.b64encode(raw).decode())

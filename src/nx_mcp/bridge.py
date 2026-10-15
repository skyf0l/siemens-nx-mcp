"""Local bridge primitives shared by the NX process and MCP sidecar."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import secrets
import select
import socket
import socketserver
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any, Literal
from uuid import uuid4

from nx_mcp import side_effects
from nx_mcp.core.errors import NXError

ObjectKind = Literal[
    "reference_set",
    "datum",
    "part",
    "sketch",
    "curve",
    "point",
    "feature",
    "body",
    "component",
    "face",
    "edge",
    "section",
    "constraint",
    "expression",
    "component_pattern",
    "assembly_constraint",
    "drawing_sheet",
    "drawing_view",
    "dimension",
    "annotation",
    "traceline",
    "explosion",
    "modeling_view",
    "drawing_table",
    "sketch_constraint",
]


@dataclass(frozen=True)
class ObjectRef:
    """One registered NX object: opaque ``id``, kind, display name and owning part."""

    id: str
    kind: ObjectKind
    name: str
    part_id: str

    def model_dump(self) -> dict[str, str]:
        """Match the sidecar model interface without importing Pydantic in NX."""
        return asdict(self)


BRIDGE_PROTOCOL_VERSION = 1
LISTEN_BACKLOG = 64
# A refused or reset connect (nothing was sent) is retried for this long while the host
# is alive (its heartbeat is fresh, or a development reload is under way).
CONNECT_RETRY_SECONDS = 5.0
CONNECT_FAILURES = (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)
_MAX_MESSAGE_BYTES = 1024 * 1024
# NX answers a request it could not finish within DISPATCHER_TIMEOUT of starting
# it itself (NX_BRIDGE_UNAVAILABLE with a known outcome). The interactive
# host may hold a request in its queue for up to QUEUE_TIMEOUT first (the engineer
# is busy in an NX command, or has not answered the panel yet); it answers held
# requests itself before that. The sidecar waits a little longer than both so
# NX's answer arrives before its own transport timeout.
DISPATCHER_TIMEOUT = 120.0
QUEUE_TIMEOUT = 135.0
CLIENT_TIMEOUT = QUEUE_TIMEOUT + DISPATCHER_TIMEOUT + 15.0
# A heavy or long call may run for its cost budget (core/tx.py COST_BUDGET_SECONDS: 300
# and 1800 s, enforced at checkpoints) plus 30 s, so its caller gets the result instead
# of an ``unknown`` outcome at DISPATCHER_TIMEOUT (red team A28). Light calls keep
# DISPATCHER_TIMEOUT.
COST_RUN_SECONDS = {"heavy": 330.0, "long": 1830.0}
# A read (catalogue effect ``read``) that NX has not started within this long fails fast
# with NX_BUSY (nothing ran; retry), instead of waiting QUEUE_TIMEOUT behind a long step
# or a blocked NX: it changes nothing, so retrying it later is always safe.
READ_QUEUE_TIMEOUT = 30.0


def _cost(method: str) -> str | None:
    """The catalogue cost class of ``method`` (None: not a loaded catalogue tool)."""
    try:
        from nx_mcp.core import catalogue

        spec = catalogue.get(method)
    except Exception:
        return None
    return getattr(spec, "cost", None)


def _effect(method: str) -> str | None:
    """The catalogue effect of ``method`` (None: not a loaded catalogue tool)."""
    try:
        from nx_mcp.core import catalogue

        spec = catalogue.get(method)
    except Exception:
        return None
    return getattr(spec, "effect", None)


def queue_timeout(method: str, base: float = QUEUE_TIMEOUT) -> float:
    """How long ``method`` may wait in NX's queue before it is answered NX_BUSY: a read
    at most READ_QUEUE_TIMEOUT, any other call ``base``."""
    return min(base, READ_QUEUE_TIMEOUT) if _effect(method) == "read" else base


def run_timeout(method: str, base: float = DISPATCHER_TIMEOUT) -> float:
    """How long NX may run ``method`` once started before its caller stops waiting."""
    return max(base, COST_RUN_SECONDS.get(_cost(method) or "", 0.0))


def client_timeout(method: str) -> float:
    """How long the sidecar waits for ``method``: the queue, the run and a margin, so
    NX's own answer always arrives first."""
    return QUEUE_TIMEOUT + run_timeout(method) + 15.0


def start_deadline(now: float | None = None) -> float:
    """``expires_at`` the sidecar gives a changing call (epoch seconds): it starts within
    QUEUE_TIMEOUT of being sent or never (core/target.py ``check_expiry``), so a call
    that sat in NX's listen backlog or queue after its client stopped waiting never runs."""
    return (time.time() if now is None else now) + QUEUE_TIMEOUT


# The socket of the request a bridge worker thread is handling (_BridgeRequestHandler):
# MainThreadDispatcher.call keeps it on the queued call, so the NX thread sees that the
# client went away (closed its socket: an MCP cancellation) before running the call.
_peer = threading.local()


def peer_closed(sock: Any) -> bool:
    """Whether the client closed ``sock`` (readable with nothing to read, or reset).
    Never blocks; an unknown socket (None) counts as open."""
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def client_gone() -> NXError:
    """The answer for a queued call whose client closed its socket before it started."""
    return NXError(
        "NX_CALL_EXPIRED",
        "The client gave up on the call (closed its connection) before NX could run it, "
        "so it did not run",
        hint="Send the call again if it is still wanted",
        retry="after_fix",
        details={"mutation_outcome": "not_started"},
    )


@dataclass
class BridgeDescriptor:
    port: int
    token: str
    pid: int
    nx_version: str
    protocol_version: int = BRIDGE_PROTOCOL_VERSION
    host: str = "127.0.0.1"

    @classmethod
    def create(
        cls,
        port: int,
        nx_version: str,
        *,
        token: str | None = None,
        pid: int | None = None,
    ) -> BridgeDescriptor:
        return cls(
            port=port,
            token=token or secrets.token_hex(32),
            pid=pid or os.getpid(),
            nx_version=nx_version,
        )

    @classmethod
    def read(cls, path: str | Path) -> BridgeDescriptor:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Bridge descriptor must be a JSON object")
        try:
            return cls(**payload)
        except TypeError as error:
            raise ValueError("Bridge descriptor is invalid") from error

    def write(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            temporary.chmod(0o600)
        temporary.replace(destination)


def default_descriptor_path() -> Path:
    if configured := os.environ.get("NX_MCP_BRIDGE_DESCRIPTOR"):
        return Path(configured)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "nx-mcp" / "bridge.json"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "nx-mcp" / "bridge.json"
    return Path.home() / ".local" / "state" / "nx-mcp" / "bridge.json"


def client_descriptor_path() -> Path:
    """The descriptor a sidecar reads: ``NX_MCP_BRIDGE_DESCRIPTOR``, else the graphical
    NX host's ``interactive-bridge.json`` when it exists, else the batch ``bridge.json``."""
    if configured := os.environ.get("NX_MCP_BRIDGE_DESCRIPTOR"):
        return Path(configured)
    batch = default_descriptor_path()
    interactive = batch.with_name("interactive-bridge.json")
    return interactive if interactive.exists() else batch


def _error_payload(error: NXError) -> dict[str, Any]:
    return error.as_dict()


class _BridgeTCPServer(socketserver.TCPServer):
    allow_reuse_address = True
    # Connections the OS holds until the host accepts them: the interactive host accepts
    # on NX's UI thread, which a long NX step holds, so a burst of parallel calls must
    # wait in the backlog rather than be refused (socketserver's default is 5).
    request_queue_size = LISTEN_BACKLOG

    executor: Any
    token: str

    def __init__(
        self,
        executor: Any,
        token: str,
        result_directory: Path | None = None,
        progress: Event | None = None,
    ) -> None:
        self.result_directory = result_directory
        self.executor = executor
        self.token = token
        # Connections accepted whose response is not written yet; ``progress`` is
        # set whenever one finishes (the interactive host waits on it, see
        # BridgeServer.in_flight).
        self.in_flight = 0
        self.progress = progress or Event()
        self._flight_lock = Lock()
        super().__init__(("127.0.0.1", 0), _BridgeRequestHandler)

    def _enter(self) -> None:
        with self._flight_lock:
            self.in_flight += 1

    def _leave(self) -> None:
        with self._flight_lock:
            self.in_flight -= 1
        self.progress.set()


class _BridgeRequestHandler(socketserver.StreamRequestHandler):
    server: _BridgeTCPServer

    def handle(self) -> None:
        raw = self.rfile.readline(_MAX_MESSAGE_BYTES + 1)
        request_id: str | None = None
        try:
            if len(raw) > _MAX_MESSAGE_BYTES:
                raise NXError("NX_REQUEST_TOO_LARGE", "Bridge request is too large")
            request = json.loads(raw)
            request_id = request.get("id")
            if request.get("jsonrpc") != "2.0":
                raise NXError("NX_PROTOCOL_MISMATCH", "Expected JSON-RPC 2.0")
            if request.get("protocol_version") != BRIDGE_PROTOCOL_VERSION:
                raise NXError("NX_PROTOCOL_MISMATCH", "Bridge protocol version does not match")
            if not hmac.compare_digest(str(request.get("token", "")), self.server.token):
                raise NXError("NX_AUTH_FAILED", "Bridge authentication failed")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise NXError("NX_INVALID_ARGUMENT", "Bridge method and params are invalid")
            _peer.socket = self.connection
            try:
                result = self.server.executor(method, params)
            finally:
                _peer.socket = None
            from nx_mcp.result_transport import bound_result

            result = bound_result(result, self.server.result_directory)
            response = {
                "jsonrpc": "2.0",
                "protocol_version": BRIDGE_PROTOCOL_VERSION,
                "id": request_id,
                "ok": True,
                "result": result,
            }
        except NXError as error:
            response = {
                "jsonrpc": "2.0",
                "protocol_version": BRIDGE_PROTOCOL_VERSION,
                "id": request_id,
                "ok": False,
                "error": _error_payload(error),
            }
        except Exception as error:
            response = {
                "jsonrpc": "2.0",
                "protocol_version": BRIDGE_PROTOCOL_VERSION,
                "id": request_id,
                "ok": False,
                "error": NXError("NX_BUILD_FAILED", str(error)).as_dict(),
            }
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")


class _ThreadedBridgeTCPServer(socketserver.ThreadingMixIn, _BridgeTCPServer):
    daemon_threads = True

    def process_request(self, request: Any, client_address: Any) -> None:
        self._enter()
        try:
            socketserver.ThreadingMixIn.process_request(self, request, client_address)
        except BaseException:
            self._leave()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            socketserver.ThreadingMixIn.process_request_thread(self, request, client_address)
        finally:
            self._leave()


class BridgeServer:
    """A serialized loopback JSON-RPC server for an NX-side executor."""

    def __init__(
        self,
        executor: Any,
        *,
        token: str,
        result_directory: Path | None = None,
        concurrent_requests: bool = False,
        progress: Event | None = None,
    ) -> None:
        # Interactive callers serialize NXOpen through MainThreadDispatcher.
        # Concurrent socket handling permits cached health reads during work;
        # the batch/default executor retains its original serialized transport.
        server_type = _ThreadedBridgeTCPServer if concurrent_requests else _BridgeTCPServer
        self._server = server_type(executor, token, result_directory, progress)
        self._thread: Thread | None = None
        self._external_accept = False
        self._closed = False

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def socket(self) -> Any:
        """The listening socket (the interactive host registers it for FD_ACCEPT)."""
        return self._server.socket

    @property
    def in_flight(self) -> int:
        """Connections accepted whose response has not been written yet."""
        return self._server.in_flight

    @property
    def progress(self) -> Event:
        return self._server.progress

    def start(self, *, accept_thread: bool = True) -> None:
        """Serve. ``accept_thread=False``: the owner accepts with accept_pending()."""
        if self._thread is not None or self._external_accept:
            return
        if not accept_thread:
            self._external_accept = True
            return
        self._thread = Thread(target=self._server.serve_forever, name="nx-mcp-bridge", daemon=True)
        self._thread.start()

    def pending_connection(self) -> bool:
        """True when a connection waits to be accepted; never blocks."""
        try:
            readable, _, _ = select.select([self._server.socket], [], [], 0)
        except (OSError, ValueError):
            return False
        return bool(readable)

    def accept_pending(self, prepare: Callable[[Any], None] | None = None, limit: int = 16) -> int:
        """Accept waiting connections on the calling thread; each is handled on its own
        worker thread. ``prepare(socket)`` runs first; a socket it rejects is closed."""
        accepted = 0
        while accepted < limit and not self._closed:
            try:
                request, address = self._server.socket.accept()
            except OSError:  # BlockingIOError: nothing waiting; others: socket closed.
                break
            try:
                if prepare is not None:
                    prepare(request)
                self._server.process_request(request, address)
            except Exception:
                with contextlib.suppress(OSError):
                    request.close()
                continue
            accepted += 1
        return accepted

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        if not self._closed:
            self._closed = True
            self._server.server_close()


@dataclass
class _PendingBridgeCall:
    method: str
    params: dict[str, Any]
    complete: Event = field(default_factory=Event)
    result: dict[str, Any] | None = None
    error: Exception | None = None
    started: bool = False
    cancelled: bool = False
    # Monotonic times: queued, first seen by the host, execution started.
    queued_at: float = field(default_factory=time.monotonic)
    seen_at: float | None = None
    started_at: float | None = None
    # The engineer approved this call on the NX MCP panel.
    approved: bool = False
    summary: str | None = None
    # A caller is blocked on this call (counted in MainThreadDispatcher.parked).
    parked: bool = False
    # Answered early by ``MainThreadDispatcher.fail_running``; the late result is dropped.
    answered: bool = False
    # The client's socket (None outside a bridge worker): closed = the client gave up.
    peer: Any = None
    # Monotonic time the request card first showed this call (Ask me first): its clock.
    shown_at: float | None = None


class MainThreadDispatcher:
    """Queues bridge calls for explicit execution by the NX journal thread."""

    def __init__(
        self,
        executor: Callable[[str, dict[str, Any]], dict[str, Any]],
        *,
        timeout: float = DISPATCHER_TIMEOUT,
        queue_timeout: float | None = None,
        progress: Event | None = None,
        run_timeout: Callable[[str], float] | None = None,
        queue_timeout_for: Callable[[str], float] | None = None,
    ) -> None:
        """``timeout`` bounds a call from its start (``run_timeout(method)`` instead when
        given, e.g. :func:`run_timeout` per cost class), ``queue_timeout`` (default:
        ``timeout``) its wait in the queue, ``queue_timeout_for(method)`` (e.g.
        :func:`queue_timeout`: reads fail fast) instead when given. ``progress`` is set
        whenever a call is queued."""
        self._executor = executor
        self._timeout = timeout
        self._run_timeout = run_timeout
        self._queue_timeout = timeout if queue_timeout is None else queue_timeout
        self._queue_timeout_for = queue_timeout_for
        self._calls: Queue[_PendingBridgeCall] = Queue()
        self._lock = Lock()
        self._count_lock = Lock()
        self._stopped = False
        self.progress = progress or Event()
        # Callers blocked on a call that has not completed: they need no CPU (or
        # Python's GIL) until it does. The interactive host compares this with the
        # server's connections in flight to tell when a worker wants to run.
        self.parked = 0
        self._running: _PendingBridgeCall | None = None
        # ``explain(pending, started)``: the host's error for a call that timed out (a
        # modal NX dialog blocks NX), or None for the bridge's own NX_BUSY/unknown answer.
        self.explain: Callable[[_PendingBridgeCall, bool], NXError | None] | None = None

    def _park(self, pending: _PendingBridgeCall) -> None:
        with self._count_lock:
            if not pending.parked:
                pending.parked = True
                self.parked += 1

    def _unpark(self, pending: _PendingBridgeCall) -> None:
        with self._count_lock:
            if pending.parked:
                pending.parked = False
                self.parked -= 1

    def _finish(self, pending: _PendingBridgeCall) -> None:
        self._unpark(pending)
        pending.complete.set()

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        pending = _PendingBridgeCall(method, params, peer=getattr(_peer, "socket", None))
        with self._lock:
            if self._stopped:
                raise NXError(
                    "NX_BRIDGE_UNAVAILABLE",
                    "NX bridge is stopping.",
                    retryable=True,
                    details={"mutation_outcome": "not_started"},
                )
            self._park(pending)
            self._calls.put(pending)
        self.progress.set()
        try:
            self._wait(pending)
        finally:
            self._unpark(pending)
        if pending.error is not None:
            raise pending.error
        if pending.result is None:
            raise NXError("NX_BUILD_FAILED", "NX bridge returned no result")
        return pending.result

    def _wait(self, pending: _PendingBridgeCall) -> None:
        """Wait up to the queue timeout for the call to start, then up to the
        dispatcher timeout from its start for it to finish."""
        waits = self._queue_wait(pending.method)
        deadline = pending.queued_at + waits
        limit = self._run_timeout(pending.method) if self._run_timeout else self._timeout
        while not pending.complete.wait(max(0.0, deadline - time.monotonic())):
            with self._lock:
                started = pending.started
                if not started:
                    pending.cancelled = True
                else:
                    deadline = (pending.started_at or pending.queued_at) + limit
            if started and time.monotonic() < deadline:
                continue
            explained = self._explain(pending, started)
            if explained is not None:
                raise explained
            if started:
                raise NXError(
                    "NX_OUTCOME_UNKNOWN",
                    f"NX started this call {limit:.0f} s ago and has not finished it: it may "
                    "still be running (nx_get_status shows what NX runs). The bridge is "
                    "connected; query nx_get_operation with the operation_id before retrying.",
                    retryable=False,
                    details={"mutation_outcome": "unknown"},
                )
            raise NXError(
                "NX_BUSY",
                f"The call waited {waits:.0f} s in NX's queue and did not start: "
                f"{self._busy_with(pending)}. Nothing ran; retry when NX is free.",
                hint="NX and the bridge are running; nx_get_status shows what NX runs. "
                "Retry later (the call did not run)",
                retryable=True,
                details={"mutation_outcome": "not_started", **self._running_row(pending)},
            )

    def _queue_wait(self, method: str) -> float:
        """How long ``method`` may wait in the queue (never raises)."""
        if self._queue_timeout_for is not None:
            try:
                return float(self._queue_timeout_for(method))
            except Exception:
                pass
        return self._queue_timeout

    def _explain(self, pending: _PendingBridgeCall, started: bool) -> NXError | None:
        """The host's own answer for a call that timed out (``explain``: a modal NX
        dialog blocks it), or None; never raises."""
        if self.explain is None:
            return None
        try:
            return self.explain(pending, started)
        except Exception:
            return None

    def _running_row(self, pending: _PendingBridgeCall) -> dict[str, Any]:
        """``{"running": {tool, seconds}}`` of the call NX runs instead (never its
        operation_id: it may be another agent's), or {}."""
        running = self._running
        if running is None or running is pending:
            return {}
        since = running.started_at or running.queued_at
        return {"running": {"tool": running.method, "seconds": round(time.monotonic() - since)}}

    def _busy_with(self, pending: _PendingBridgeCall) -> str:
        row = self._running_row(pending).get("running")
        if row:
            return f"NX is running and busy with {row['tool']} (running for {row['seconds']} s)"
        return (
            "NX is running but did not take it (the engineer was busy in NX, or calls ahead "
            "of it waited)"
        )

    def drain(self, timeout: float = 0.0, *, limit: int = 1) -> int:
        """Execute up to ``limit`` pending calls on the calling thread."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        try:
            pending = self._calls.get(timeout=timeout)
        except Empty:
            return 0

        processed = 0
        while True:
            self._execute(pending)
            processed += 1
            if processed == limit:
                return processed
            try:
                pending = self._calls.get_nowait()
            except Empty:
                return processed

    def waiting(self) -> list[_PendingBridgeCall]:
        """Queued calls that have not started or expired, oldest first. Expired calls
        (their caller already has its answer) leave the queue here."""
        with self._calls.mutex:
            queue = self._calls.queue
            for pending in [p for p in queue if p.cancelled]:
                queue.remove(pending)
            return list(queue)

    def drain_where(self, accept: Callable[[_PendingBridgeCall], bool]) -> int:
        """Execute the oldest queued call that ``accept`` allows; leave the rest queued."""
        with self._calls.mutex:
            pending = next((p for p in self._calls.queue if accept(p)), None)
            if pending is None:
                return 0
            self._calls.queue.remove(pending)
        self._execute(pending)
        return 1

    def reject_where(self, reject: Callable[[_PendingBridgeCall], bool], error: NXError) -> int:
        """Complete matching queued calls with ``error`` without executing them."""
        with self._calls.mutex:
            rejected = [p for p in self._calls.queue if reject(p)]
            for pending in rejected:
                self._calls.queue.remove(pending)
        for pending in rejected:
            pending.error = NXError(
                error.code,
                error.message,
                hint=error.hint,
                retryable=error.retryable,
                retry=error.retry,
                details={**error.details, "mutation_outcome": "not_started"},
            )
            self._finish(pending)
        return len(rejected)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            while True:
                try:
                    pending = self._calls.get_nowait()
                except Empty:
                    return
                pending.error = NXError(
                    "NX_BRIDGE_UNAVAILABLE",
                    "NX bridge is stopping.",
                    retryable=True,
                    details={"mutation_outcome": "not_started"},
                )
                self._finish(pending)

    def _execute(self, pending: _PendingBridgeCall) -> None:
        with self._lock:
            if self._stopped or pending.cancelled:
                pending.error = NXError(
                    "NX_BRIDGE_UNAVAILABLE",
                    "Bridge stopped or queued request expired before execution.",
                    retryable=True,
                    details={"mutation_outcome": "not_started"},
                )
                self._finish(pending)
                return
            if peer_closed(pending.peer):  # the client gave up (MCP cancel): never a ghost
                pending.error = client_gone()
                self._finish(pending)
                return
            pending.started = True
            pending.started_at = time.monotonic()
            self._running = pending
        # The queue consumer is serialized; release the queue lock so a waiting
        # caller can distinguish queued expiry from a running, unknown outcome.
        result: dict[str, Any] | None = None
        failure: Exception | None = None
        try:
            result = self._executor(pending.method, pending.params)
        except Exception as error:
            failure = error
        finally:
            with self._lock:
                self._running = None
                if not pending.answered:
                    pending.result, pending.error = result, failure
            self._finish(pending)

    def fail_running(self, error: Exception) -> bool:
        """Answer the running call now with ``error`` (from another thread).

        For a call that cannot finish on its own, such as one waiting behind a modal
        NX dialog: its caller gets ``error`` at once, and the result the NX thread
        produces later is dropped. False when no call is running."""
        with self._lock:
            pending = self._running
            if pending is None or pending.answered or pending.complete.is_set():
                return False
            pending.answered = True
            pending.result, pending.error = None, error
        self._finish(pending)
        return True


class BridgeClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        token: str,
        timeout: float = CLIENT_TIMEOUT,
        per_cost: bool = False,
    ) -> None:
        """``timeout``: how long a call waits for NX; ``per_cost``: at least
        :func:`client_timeout` of the method (heavy and long calls wait longer)."""
        if host != "127.0.0.1":
            raise ValueError("NX bridge must use the 127.0.0.1 loopback address")
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self.per_cost = per_cost

    def timeout_for(self, method: str) -> float:
        return max(self.timeout, client_timeout(method)) if self.per_cost else self.timeout

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = uuid4().hex
        request = {
            "jsonrpc": "2.0",
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "id": request_id,
            "token": self.token,
            "method": method,
            "params": params,
        }
        writer: asyncio.StreamWriter | None = None
        sent = connected = False
        timeout = self.timeout_for(method)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.host,
                    self.port,
                    limit=_MAX_MESSAGE_BYTES + 1,
                ),
                timeout=timeout,
            )
            assert writer is not None
            connected = True
            writer.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
            sent = True
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except ValueError as error:
            raise NXError(
                "NX_RESPONSE_TOO_LARGE",
                "Bridge response exceeded framing limit; inspect operation status before retry",
                details={"operation_id": params.get("operation_id"), "mutation_outcome": "unknown"},
            ) from error
        except (OSError, asyncio.TimeoutError) as error:
            if sent:
                # NX may be running or have committed the request: never retry blindly.
                raise NXError(
                    "NX_OUTCOME_UNKNOWN",
                    "The request reached the NX bridge but no answer arrived "
                    f"({type(error).__name__}: {error}). Query nx_get_operation with this "
                    "operation_id before retrying.",
                    retryable=False,
                    details={
                        "operation_id": params.get("operation_id"),
                        "mutation_outcome": "unknown",
                    },
                ) from error
            details: dict[str, Any] = {"mutation_outcome": "not_started"}
            if not connected and isinstance(error, CONNECT_FAILURES):
                details["connect"] = "refused"  # no byte was written: a retry is safe
            raise NXError(
                "NX_BRIDGE_UNAVAILABLE",
                f"Cannot connect to the NX bridge at {self.host}:{self.port} "
                f"({type(error).__name__}: {error}). Nothing ran.",
                hint="nx_get_status says whether NX and its NX MCP host run; retry when they do",
                retryable=True,
                details=details,
            ) from error
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()

        if not raw or len(raw) > _MAX_MESSAGE_BYTES:
            # The request was delivered; a dropped connection leaves its outcome unknown.
            raise NXError(
                "NX_PROTOCOL_MISMATCH",
                "NX bridge returned an invalid response size; query nx_get_operation "
                "before retrying",
                retryable=False,
                details={"operation_id": params.get("operation_id"), "mutation_outcome": "unknown"},
            )
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as parse_error:
            raise NXError(
                "NX_PROTOCOL_MISMATCH", "NX bridge returned invalid JSON"
            ) from parse_error
        if (
            response.get("id") != request_id
            or response.get("protocol_version") != BRIDGE_PROTOCOL_VERSION
        ):
            raise NXError("NX_PROTOCOL_MISMATCH", "NX bridge returned an invalid response")
        if not response.get("ok"):
            error_payload = response.get("error", {})
            raise NXError(
                error_payload.get("code", "NX_BUILD_FAILED"),
                error_payload.get("message", "NX operation failed"),
                hint=error_payload.get("suggestion"),
                nx_code=error_payload.get("nx_code"),
                retryable=error_payload.get("retryable", False),
                details=error_payload.get("details"),
            )
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise NXError("NX_PROTOCOL_MISMATCH", "NX bridge result must be an object")
        return result


class DescriptorBridgeClient:
    """Reloads the bridge descriptor for every call so NX can restart independently."""

    def __init__(
        self,
        descriptor_path: str | Path | None = None,
        *,
        timeout: float = CLIENT_TIMEOUT,
        per_cost: bool = True,
    ) -> None:
        self._descriptor_path = Path(descriptor_path) if descriptor_path else None
        self.timeout = timeout
        self.per_cost = per_cost

    @property
    def descriptor_path(self) -> Path:
        """The given path, else ``client_descriptor_path()`` at each call (NX may start
        after the sidecar)."""
        return self._descriptor_path or client_descriptor_path()

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            descriptor = BridgeDescriptor.read(self.descriptor_path)
        except (OSError, ValueError) as error:
            raise NXError(
                "NX_BRIDGE_UNAVAILABLE",
                "No NX bridge is registered (no bridge descriptor): NX with the NX MCP host is "
                "not running. Start it inside Siemens NX before calling tools. Nothing ran.",
                retryable=True,
                details={"mutation_outcome": "not_started"},
            ) from error
        if descriptor.protocol_version != BRIDGE_PROTOCOL_VERSION:
            raise NXError(
                "NX_PROTOCOL_MISMATCH",
                "NX bridge and sidecar use different protocol versions.",
            )
        return await BridgeClient(
            descriptor.host,
            descriptor.port,
            token=descriptor.token,
            timeout=self.timeout,
            per_cost=self.per_cost,
        ).call(method, params)


@dataclass
class _ObjectEntry:
    value: Any
    reference: ObjectRef


class ObjectRegistry:
    """Maps opaque, session-scoped IDs to live NXOpen objects."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self._objects: dict[str, _ObjectEntry] = {}
        self._stale_ids: set[str] = set()
        self._identities: dict[tuple[str, ObjectKind, str], str] = {}

    def register(self, value: Any, *, kind: ObjectKind, name: str, part_id: str) -> ObjectRef:
        native_identity = getattr(value, "Tag", None)
        identity = str(native_identity if native_identity is not None else id(value))
        identity_key = (part_id, kind, identity)
        if object_id := self._identities.get(identity_key):
            entry = self._objects[object_id]
            # NX may reuse a tag after an undo inside one call (a rolled-back attempt):
            # keep the live object, never a wrapper of the deleted one.
            entry.value = value
            if entry.reference.name != name:
                entry.reference = replace(entry.reference, name=name)
            return entry.reference
        reference = ObjectRef(
            id=f"obj_{getattr(self, 'session_id', '') + '_' if getattr(self, 'session_id', None) else ''}{uuid4().hex}",
            kind=kind,
            name=name,
            part_id=part_id,
        )
        self._objects[reference.id] = _ObjectEntry(value=value, reference=reference)
        self._identities[identity_key] = reference.id
        return reference

    def display_name(self, object_id: str) -> str | None:
        """Registered name (or kind) of a live ID, without touching NXOpen."""
        entry = self._objects.get(object_id)
        if entry is None:
            return None
        return entry.reference.name or entry.reference.kind

    def resolve(
        self,
        object_id: str,
        *,
        expected_kind: ObjectKind | None = None,
        part_id: str | None = None,
    ) -> Any:
        entry = self._objects.get(object_id)
        if entry is None:
            foreign_session = bool(
                self.session_id is not None
                and not object_id.startswith("obj_" + self.session_id + "_")
            )
            code = (
                "NX_STALE_REFERENCE"
                if object_id in self._stale_ids or foreign_session
                else "NX_NOT_FOUND"
            )
            raise NXError(code, f"Object reference is not valid: {object_id}")
        if expected_kind is not None and entry.reference.kind != expected_kind:
            raise NXError(
                "NX_WRONG_KIND",
                f"Expected {expected_kind}, got {entry.reference.kind}",
            )
        if part_id is not None and entry.reference.part_id != part_id:
            raise NXError(
                "NX_STALE_REFERENCE",
                "Object reference belongs to a different work part",
            )
        side_effects.touch()  # The caller now holds a live NX object.
        return entry.value

    def invalidate_part(self, part_id: str) -> None:
        invalid_ids = [
            object_id
            for object_id, entry in self._objects.items()
            if entry.reference.part_id == part_id
        ]
        for object_id in invalid_ids:
            del self._objects[object_id]
            self._stale_ids.add(object_id)
        self._identities = {
            key: object_id for key, object_id in self._identities.items() if key[0] != part_id
        }

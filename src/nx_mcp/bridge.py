"""Local bridge primitives shared by the NX process and MCP sidecar."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import secrets
import select
import socketserver
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any, Literal
from uuid import uuid4

from nx_mcp import side_effects
from nx_mcp.core.errors import NXToolError

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
_MAX_MESSAGE_BYTES = 1024 * 1024
# NX answers a request it could not finish within DISPATCHER_TIMEOUT of starting
# it itself (NX_MAIN_THREAD_UNAVAILABLE with a known outcome). The interactive
# host may hold a request in its queue for up to QUEUE_TIMEOUT first (the engineer
# is busy in an NX command, or has not answered the panel yet); it answers held
# requests itself before that. The sidecar waits a little longer than both so
# NX's answer arrives before its own transport timeout.
DISPATCHER_TIMEOUT = 120.0
QUEUE_TIMEOUT = 135.0
CLIENT_TIMEOUT = QUEUE_TIMEOUT + DISPATCHER_TIMEOUT + 15.0


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


def _error_payload(error: NXToolError) -> dict[str, Any]:
    return error.as_dict()


class _BridgeTCPServer(socketserver.TCPServer):
    allow_reuse_address = True

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
                raise NXToolError("NX_REQUEST_TOO_LARGE", "Bridge request is too large")
            request = json.loads(raw)
            request_id = request.get("id")
            if request.get("jsonrpc") != "2.0":
                raise NXToolError("NX_PROTOCOL_ERROR", "Expected JSON-RPC 2.0")
            if request.get("protocol_version") != BRIDGE_PROTOCOL_VERSION:
                raise NXToolError(
                    "NX_PROTOCOL_VERSION_MISMATCH", "Bridge protocol version does not match"
                )
            if not hmac.compare_digest(str(request.get("token", "")), self.server.token):
                raise NXToolError("NX_AUTH_FAILED", "Bridge authentication failed")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise NXToolError("NX_INVALID_REQUEST", "Bridge method and params are invalid")
            result = self.server.executor(method, params)
            from nx_mcp.result_transport import bound_result

            result = bound_result(result, self.server.result_directory)
            response = {
                "jsonrpc": "2.0",
                "protocol_version": BRIDGE_PROTOCOL_VERSION,
                "id": request_id,
                "ok": True,
                "result": result,
            }
        except NXToolError as error:
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
                "error": NXToolError("NX_OPERATION_FAILED", str(error)).as_dict(),
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


class MainThreadDispatcher:
    """Queues bridge calls for explicit execution by the NX journal thread."""

    def __init__(
        self,
        executor: Callable[[str, dict[str, Any]], dict[str, Any]],
        *,
        timeout: float = DISPATCHER_TIMEOUT,
        queue_timeout: float | None = None,
        progress: Event | None = None,
    ) -> None:
        """``timeout`` bounds a call from its start, ``queue_timeout`` (default:
        ``timeout``) its wait in the queue. ``progress`` is set whenever a call is
        queued."""
        self._executor = executor
        self._timeout = timeout
        self._queue_timeout = timeout if queue_timeout is None else queue_timeout
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
        pending = _PendingBridgeCall(method, params)
        with self._lock:
            if self._stopped:
                raise NXToolError(
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
            raise NXToolError("NX_OPERATION_FAILED", "NX bridge returned no result")
        return pending.result

    def _wait(self, pending: _PendingBridgeCall) -> None:
        """Wait up to the queue timeout for the call to start, then up to the
        dispatcher timeout from its start for it to finish."""
        deadline = pending.queued_at + self._queue_timeout
        while not pending.complete.wait(max(0.0, deadline - time.monotonic())):
            with self._lock:
                started = pending.started
                if not started:
                    pending.cancelled = True
                else:
                    deadline = (pending.started_at or pending.queued_at) + self._timeout
            if started and time.monotonic() < deadline:
                continue
            raise NXToolError(
                "NX_MAIN_THREAD_UNAVAILABLE",
                "NX did not complete the bridge request before the timeout. Query the operation receipt before retrying a started mutation.",
                retryable=not started,
                details={"mutation_outcome": "unknown" if started else "not_started"},
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
        """Queued calls that have not started or expired, oldest first."""
        with self._calls.mutex:
            return [pending for pending in self._calls.queue if not pending.cancelled]

    def drain_where(self, accept: Callable[[_PendingBridgeCall], bool]) -> int:
        """Execute the oldest queued call that ``accept`` allows; leave the rest queued."""
        with self._calls.mutex:
            pending = next((p for p in self._calls.queue if accept(p)), None)
            if pending is None:
                return 0
            self._calls.queue.remove(pending)
        self._execute(pending)
        return 1

    def reject_where(self, reject: Callable[[_PendingBridgeCall], bool], error: NXToolError) -> int:
        """Complete matching queued calls with ``error`` without executing them."""
        with self._calls.mutex:
            rejected = [p for p in self._calls.queue if reject(p)]
            for pending in rejected:
                self._calls.queue.remove(pending)
        for pending in rejected:
            pending.error = NXToolError(
                error.code,
                error.message,
                retryable=error.retryable,
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
                pending.error = NXToolError(
                    "NX_BRIDGE_UNAVAILABLE",
                    "NX bridge is stopping.",
                    retryable=True,
                    details={"mutation_outcome": "not_started"},
                )
                self._finish(pending)

    def _execute(self, pending: _PendingBridgeCall) -> None:
        with self._lock:
            if self._stopped or pending.cancelled:
                pending.error = NXToolError(
                    "NX_BRIDGE_UNAVAILABLE",
                    "Bridge stopped or queued request expired before execution.",
                    retryable=True,
                    details={"mutation_outcome": "not_started"},
                )
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
        self, host: str, port: int, *, token: str, timeout: float = CLIENT_TIMEOUT
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("NX bridge must use the 127.0.0.1 loopback address")
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout

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
        sent = False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.host,
                    self.port,
                    limit=_MAX_MESSAGE_BYTES + 1,
                ),
                timeout=self.timeout,
            )
            assert writer is not None
            writer.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
            sent = True
            raw = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
        except ValueError as error:
            raise NXToolError(
                "NX_RESPONSE_TOO_LARGE",
                "Bridge response exceeded framing limit; inspect operation status before retry",
                details={"operation_id": params.get("operation_id"), "mutation_outcome": "unknown"},
            ) from error
        except (OSError, asyncio.TimeoutError) as error:
            if sent:
                # NX may be running or have committed the request: never retry blindly.
                raise NXToolError(
                    "NX_BRIDGE_OUTCOME_UNKNOWN",
                    "The request reached the NX bridge but no answer arrived "
                    f"({type(error).__name__}: {error}). Query nx_get_operation with this "
                    "operation_id before retrying.",
                    retryable=False,
                    details={
                        "operation_id": params.get("operation_id"),
                        "mutation_outcome": "unknown",
                    },
                ) from error
            raise NXToolError(
                "NX_BRIDGE_UNAVAILABLE",
                f"NX bridge is unavailable: {error}",
                retryable=True,
                details={"mutation_outcome": "not_started"},
            ) from error
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()

        if not raw or len(raw) > _MAX_MESSAGE_BYTES:
            # The request was delivered; a dropped connection leaves its outcome unknown.
            raise NXToolError(
                "NX_PROTOCOL_ERROR",
                "NX bridge returned an invalid response size; query nx_get_operation "
                "before retrying",
                details={"operation_id": params.get("operation_id"), "mutation_outcome": "unknown"},
            )
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as parse_error:
            raise NXToolError(
                "NX_PROTOCOL_ERROR", "NX bridge returned invalid JSON"
            ) from parse_error
        if (
            response.get("id") != request_id
            or response.get("protocol_version") != BRIDGE_PROTOCOL_VERSION
        ):
            raise NXToolError("NX_PROTOCOL_ERROR", "NX bridge returned an invalid response")
        if not response.get("ok"):
            error_payload = response.get("error", {})
            raise NXToolError(
                error_payload.get("code", "NX_OPERATION_FAILED"),
                error_payload.get("message", "NX operation failed"),
                suggestion=error_payload.get("suggestion"),
                nx_code=error_payload.get("nx_code"),
                retryable=error_payload.get("retryable", False),
                details=error_payload.get("details"),
            )
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise NXToolError("NX_PROTOCOL_ERROR", "NX bridge result must be an object")
        return result


class DescriptorBridgeClient:
    """Reloads the bridge descriptor for every call so NX can restart independently."""

    def __init__(
        self, descriptor_path: str | Path | None = None, *, timeout: float = CLIENT_TIMEOUT
    ) -> None:
        self.descriptor_path = (
            Path(descriptor_path) if descriptor_path else default_descriptor_path()
        )
        self.timeout = timeout

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            descriptor = BridgeDescriptor.read(self.descriptor_path)
        except (OSError, ValueError) as error:
            raise NXToolError(
                "NX_BRIDGE_UNAVAILABLE",
                "Start the NX MCP bridge inside Siemens NX before calling tools.",
                retryable=True,
                details={"mutation_outcome": "not_started"},
            ) from error
        if descriptor.protocol_version != BRIDGE_PROTOCOL_VERSION:
            raise NXToolError(
                "NX_PROTOCOL_VERSION_MISMATCH",
                "NX bridge and sidecar use different protocol versions.",
            )
        return await BridgeClient(
            descriptor.host,
            descriptor.port,
            token=descriptor.token,
            timeout=self.timeout,
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
                "NX_OBJECT_STALE"
                if object_id in self._stale_ids or foreign_session
                else "NX_OBJECT_NOT_FOUND"
            )
            raise NXToolError(code, f"Object reference is not valid: {object_id}")
        if expected_kind is not None and entry.reference.kind != expected_kind:
            raise NXToolError(
                "NX_OBJECT_TYPE_MISMATCH",
                f"Expected {expected_kind}, got {entry.reference.kind}",
            )
        if part_id is not None and entry.reference.part_id != part_id:
            raise NXToolError(
                "NX_OBJECT_STALE",
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

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
import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from mcp.types import CallToolResult, ImageContent, ResourceLink, TextContent
from pydantic import AnyUrl, ValidationError

from nx_mcp.core import errors, evidence
from nx_mcp.core.errors import NXError
from nx_mcp.core.receipts import new_operation_id
from nx_mcp.core.validate import validate
from nx_mcp.sidecar import schema
from nx_mcp.workspace import WorkspaceViolation

if TYPE_CHECKING:
    from nx_mcp.sidecar.local import SidecarServices

COMPACT_ITEMS = 20
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


def _project(envelope: dict, store) -> dict:
    """Compact detail: arrays over 20 items go to the result store (gate 14, S)."""
    if not envelope.get("ok"):
        return envelope
    omitted: dict[str, Any] = {}
    paged = envelope.get("page") is not None

    def cut(value, path):
        if isinstance(value, dict):
            return {k: cut(v, f"{path}/{k}") for k, v in value.items()}
        if isinstance(value, list):
            if len(value) > COMPACT_ITEMS and not (paged and path == "/data/items"):
                omitted[path] = {"total": len(value), "returned": COMPACT_ITEMS}
                value = value[:COMPACT_ITEMS]
            return [cut(v, f"{path}/{i}") for i, v in enumerate(value)]
        return value

    projected = {
        k: v if k in ("warnings", "error", "page") else cut(v, "/" + k) for k, v in envelope.items()
    }
    if not omitted:
        return envelope
    try:
        result_id = store.put(envelope)
    except Exception:
        return envelope  # A storage fault never changes a finished call's answer.
    projected.update(result_id=result_id, omitted=omitted, expand_with="nx_get_result")
    return projected


STATUS_TTL = 30.0  # seconds a listing trusts the session state read from nx_get_status
STATUS_TIMEOUT = 5.0


class CatalogueProxy:
    def __init__(self, bridge, workspace, store, *, build=None) -> None:
        self.bridge = bridge
        self.workspace = workspace
        self.store = store
        self._build = build
        self._services: SidecarServices | None = None  # sidecar/local.py, made on first use
        # Session state of the connected NX, from nx_get_status: the license failures
        # it remembers. ``_key`` is the bridge descriptor it was read for.
        self._key: tuple | None = None
        self._read_at = 0.0
        self.licenses: list[dict] = []

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

    async def refresh(self, *, force: bool = False) -> None:
        """Read the session state from nx_get_status when NX (re)connected, when the
        last read is older than STATUS_TTL, or when ``force``; never raises."""
        key = self.connected_key()
        if key is None:
            self._key, self.licenses = None, []
            return
        fresh = time.monotonic() - self._read_at < STATUS_TTL
        if key == self._key and fresh and not force:
            return
        if key != self._key:
            self.licenses = []  # a new NX session remembers nothing yet
        self._key, self._read_at = key, time.monotonic()
        try:
            answer = await asyncio.wait_for(
                self.bridge.call("nx_get_status", {"include": ["bridge"]}), STATUS_TIMEOUT
            )
            rows = ((answer or {}).get("data") or {}).get("bridge", {}).get("license_unavailable")
        except Exception:
            return  # NX busy or down: keep what is known
        if isinstance(rows, list):
            self.licenses = [r for r in rows if isinstance(r, dict) and r.get("tool")]

    def signature(self) -> tuple:
        """What listings depend on; a change sends tools/list_changed."""
        rows = frozenset((r.get("tool"), r.get("variant")) for r in self.licenses)
        return (*self.listing(), self.dev(), rows)

    @staticmethod
    def dev() -> bool:
        return os.environ.get("NX_MCP_DEV") == "1"

    def remembered(self, spec, variant: str | None = None) -> dict | None:
        """The license failure NX remembers for this tool (``variant``: or that variant)."""
        return evidence.remembered(self.licenses, spec.name, variant)

    def variants(self, spec) -> tuple[str, ...]:
        """The variant values listed for ``spec``: tested on the listing build and not
        refused for a license; development mode lists every value."""
        values = spec.variant_values()
        if not values:
            return values
        build, _ = self.listing()
        usable = values if self.dev() else evidence.variants_for(spec, build)
        if not isinstance(spec.variants, str):
            return usable
        return tuple(v for v in usable if not self.remembered(spec, v))

    def unavailable(self, spec, variant: str | None = None) -> str | None:
        """Why ``spec`` (or its ``variant``) is not offered on this build, or None.
        Development mode offers untested tools and variants (marked tested: false)."""
        entry = self.remembered(spec, variant if isinstance(spec.variants, str) else None)
        if entry is not None:
            return f"no NX license on this seat (remembered from {entry.get('since')})"
        if self.dev():
            return None
        build, assumed = self.listing()
        where = f"NX build {build}" + (" (assumed: NX not connected)" if assumed else "")
        if not evidence.visible(spec, build):
            return f"not tested on {where}"
        if variant is not None and variant not in evidence.variants_for(spec, build):
            return f"variant {variant!r} is not tested on {where}"
        if spec.variant_values() and not self.variants(spec):
            return f"no variant is available on {where}"
        return None

    def visible(self, spec) -> bool:
        """Discovery filter (decision 11); development mode shows untested tools."""
        return self.unavailable(spec) is None

    def tested(self, spec) -> bool:
        return evidence.available(spec, self.listing()[0])

    def _invalid(self, spec, args) -> NXError | None:
        try:
            schema.adapter(spec).validate_python(args)
            return None
        except ValidationError as error:
            first = error.errors()[0]
            try:
                validate(spec.params, args)  # Same rules (parity test), clearer message.
            except NXError as precise:
                return precise
            field = schema.error_pointer(args, first["loc"], first["type"]) or None
            return NXError(
                "NX_INVALID_ARGUMENT", f"{field or 'arguments'}: {first['msg']}", field=field
            )

    def _resolve_paths(self, spec, args: dict) -> NXError | None:
        for pointer, _mode, _suffixes in spec.paths:
            for container, key in _walk_paths(args, pointer):
                value = container[key]
                try:
                    container[key] = str(self.workspace.resolve(value))
                except WorkspaceViolation as error:
                    reserved = ".nx-mcp" in str(value).replace("\\", "/").casefold().split("/")
                    field = pointer.replace("*", str(key)) if "*" in pointer else pointer
                    return NXError(
                        "NX_PATH_RESERVED" if reserved else "NX_PATH_OUTSIDE_WORKSPACE",
                        f"{field}: {error}",
                        field=field,
                        details={"path": value},
                    )
        return None

    async def call(
        self, spec, arguments: dict | None, *, intent: str | None = None, detail: str = "compact"
    ) -> CallToolResult:
        args = dict(arguments or {})
        operation_id = args.pop("operation_id", None)
        intent = args.pop("intent", None) or intent
        detail = args.pop("detail", None) or detail
        problem = None
        if operation_id is not None and (
            not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128
        ):
            problem = NXError(
                "NX_INVALID_ARGUMENT", "operation_id: 1-128 characters", field="/operation_id"
            )
        elif intent is not None and (not isinstance(intent, str) or len(intent) > 120):
            problem = NXError("NX_INVALID_ARGUMENT", "intent: at most 120 characters")
        elif detail not in ("compact", "full"):
            problem = NXError("NX_INVALID_ARGUMENT", "detail: compact or full", field="/detail")
        problem = problem or self._invalid(spec, args) or self._resolve_paths(spec, args)  # 1, 2
        if problem is None and not self.dev():  # 3
            build = self.build()
            if build is not None:
                problem = evidence.check(spec, build, args)
        if problem is None:  # 3: the license NX remembers failing this session
            entry = self.remembered(spec, spec.variant_of(args))
            if entry is not None:
                problem = evidence.license_error(entry)
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
            if detail == "compact":
                envelope = _project(envelope, self.store)
            return result(envelope, extra=self._attachments(envelope))
        if operation_id:
            params["operation_id"] = operation_id
        if intent:
            params["intent"] = " ".join(intent.split())
        try:
            answer = await self.bridge.call(spec.name, params)
        except NXError as error:
            envelope = from_bridge_error(spec.name, spec.effect, error, operation_id)
            if spec.name == "nx_get_status" and envelope["error"].get("category") == "bridge":
                # Status still answers with NX down: workspace rules, bridge unavailable.
                from nx_mcp.domains.session import status

                envelope = status.offline_envelope(
                    self.workspace.root, envelope["error"], args, build=self.build()
                )
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
        if detail == "compact":
            envelope = _project(envelope, self.store)
        return result(envelope, extra=self._attachments(envelope))

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

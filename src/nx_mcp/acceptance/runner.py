"""Acceptance runner: replay scenarios through a real sidecar, write and fold evidence.

A scenario is ``acceptance/<domain>/<name>.json``::

    {"covers": {"nx_create_datum_plane": ["offset", ...]},   # tool -> variants credited
     "part": "new" | null, "fixture": "fixtures/x.prt" | null,
     "steps": [{"call": tool, "args": {...}, "save": label, "setup": true,
                "expect": {"data.origin.2": {"close": 25, "tol": 1e-6}}}]}

Argument strings ``$part`` (a fresh absolute path under ``smoke/<run-id>/``),
``$fixture`` (a copy of the fixture there), ``$run`` (the run directory) and
``$label.dotted.path`` (a value from an earlier saved result) are resolved
before each call; ``$$`` escapes a literal ``$`` (``"$$0.data.sketch"`` reaches
the tool as ``"$0.data.sketch"``, an ``nx_run_batch`` step reference). Every call goes through the stdio MCP client of a fresh
``python -m nx_mcp.server`` (agent profile): catalogue read tools through
``nx_invoke_read``, everything else through ``nx_invoke`` with an intent, both
with ``detail=full`` (a step's ``"detail": "compact"`` asks for the compact
projection, to check ``result_id`` and ``omitted``); ``expect`` paths address the
envelope (``ok``, ``data``, ``error.code``/``category``, ``mutation_outcome``). Only
steps with an ``expect`` count as checks.

A run writes ``acceptance/evidence/<nx-version>/<domain>/<name>.json`` (commit,
NX build, timestamps, per-step pass/fail and result digests). ``fold`` builds
``src/nx_mcp/evidence/<domain>.json`` from passing evidence files only.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nx_mcp.acceptance import expect as expectations
from nx_mcp.core import catalogue
from nx_mcp.core import evidence as core_evidence

# The checkout (scenarios, fixtures, scripts/dev_lock.py); an installed release
# points NX_MCP_SOURCE_ROOT at its source/ folder.
ROOT = Path(os.environ.get("NX_MCP_SOURCE_ROOT") or Path(__file__).resolve().parents[3])
SCENARIOS = ROOT / "acceptance"
EVIDENCE = SCENARIOS / "evidence"
FIXTURES = SCENARIOS / "fixtures"
SPECIAL = ("$part", "$fixture", "$run")
STEP_KEYS = {"call", "args", "save", "expect", "setup", "intent", "detail"}
CLEANUP = ("close", "save", "keep")


class ScenarioError(ValueError):
    """A scenario file breaks the format."""


# ---------------------------------------------------------------------------
# Scenarios


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def load_scenario(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise ScenarioError(f"{path}: a scenario is an object with a 'steps' list")
    if data.get("cleanup", "close") not in CLEANUP:
        raise ScenarioError(f"{path}: 'cleanup' is one of {', '.join(CLEANUP)}")
    covers = data.get("covers")
    if not isinstance(covers, dict) or not all(isinstance(v, list) for v in covers.values()):
        raise ScenarioError(f"{path}: 'covers' maps tool names to variant lists")
    for index, step in enumerate(data["steps"], 1):
        if not isinstance(step, dict) or not isinstance(step.get("call"), str):
            raise ScenarioError(f"{path} step {index}: each step needs 'call'")
        unknown = set(step) - STEP_KEYS
        if unknown:
            raise ScenarioError(f"{path} step {index}: unknown keys {sorted(unknown)}")
        for target, rule in (step.get("expect") or {}).items():
            operators = isinstance(rule, dict) and set(rule) & set(expectations.OPERATORS)
            if operators and not expectations.is_operator(rule):
                raise ScenarioError(f"{path} step {index}: bad operator for {target}")
    return data


def scenario_files(names: list[str] | None = None) -> list[Path]:
    """Scenario files by ``domain``, ``domain/name`` or path; all when empty."""
    if not names:
        return sorted(
            p
            for d in sorted(catalogue.DOMAINS)
            if (SCENARIOS / d).is_dir()
            for p in (SCENARIOS / d).glob("*.json")
        )
    found: list[Path] = []
    for name in names:
        path = Path(name)
        candidates = [path, SCENARIOS / name, SCENARIOS / (name + ".json")]
        match = next((c for c in candidates if c.is_file()), None)
        if match is not None:
            found.append(match.resolve())
        elif (SCENARIOS / name).is_dir():
            found.extend(sorted((SCENARIOS / name).glob("*.json")))
        else:
            raise ScenarioError(f"No scenario {name!r}")
    return found


def coverage() -> dict[str, set[str]]:
    """``{tool: variants}`` covered by every scenario file."""
    out: dict[str, set[str]] = {}
    for path in scenario_files():
        for tool, variants in load_scenario(path)["covers"].items():
            out.setdefault(tool, set()).update(variants)
    return out


# ---------------------------------------------------------------------------
# Results


def unwrap(structured: Any) -> dict:
    """The envelope view of a result: envelopes as they are, any other dict as a
    success, anything else as an ``NX_ERROR`` failure."""
    if not isinstance(structured, dict):
        return {"ok": False, "error": {"code": "NX_ERROR", "message": repr(structured)}}
    if "ok" in structured:
        return structured
    return {**structured, "ok": True}


def resolve(value: Any, saved: dict, special: dict) -> Any:
    """Replace ``$part``/``$fixture``/``$run`` and ``$label.path`` strings (``$$`` = ``$``)."""
    if isinstance(value, str) and value.startswith("$$"):
        return value[1:]
    if isinstance(value, str) and value.startswith("$"):
        if value in special:
            return special[value]
        head, sep, tail = value.partition("/")
        if sep and head in special:  # "$run/name.prt": a path inside the run directory
            return str(Path(special[head]) / tail)
        label, _, path = value[1:].partition(".")
        if label not in saved:
            raise KeyError(f"{value}: no saved result {label!r} (saved: {sorted(saved)})")
        found = expectations.lookup(saved[label], path)
        if found is expectations._MISSING:
            raise KeyError(f"{value}: no such path in the saved result {label!r}")
        return found
    if isinstance(value, dict):
        return {k: resolve(v, saved, special) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, saved, special) for v in value]
    return value


@dataclass
class StepResult:
    index: int
    call: str
    setup: bool
    passed: bool
    checks: int
    seconds: float
    result_sha256: str | None = None
    failures: list[str] = field(default_factory=list)
    result: Any = None

    def record(self) -> dict:
        out = {
            "index": self.index,
            "call": self.call,
            "setup": self.setup,
            "passed": self.passed,
            "checks": self.checks,
            "seconds": round(self.seconds, 3),
            "result_sha256": self.result_sha256,
        }
        if self.failures:
            out["failures"] = self.failures
        return out


async def call_tool(
    session, tool: str, args: dict, *, intent: str | None, detail: str = "full"
) -> dict:
    """One call through the gateways (``detail=full`` by default); returns the unwrapped result."""
    spec = catalogue.load().get(tool)
    if spec is not None and spec.effect == "read":
        request = {"tool": tool, "arguments": args, "detail": detail}
        response = await session.call_tool("nx_invoke_read", request)
    else:
        request = {"tool": tool, "arguments": args, "detail": detail}
        if intent:
            request["intent"] = intent[:120]
        response = await session.call_tool("nx_invoke", request)
    body = response.structuredContent
    if body is None:
        text = next((getattr(c, "text", None) for c in response.content), None)
        try:
            body = json.loads(text) if text else None
        except ValueError:
            body = text
    return unwrap(body)


def _digest(result: Any) -> str:
    return sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, default=str).encode())


async def run_steps(session, steps: list[dict], special: dict, *, label: str, echo=None):
    """Run ``steps`` in order; returns a list of :class:`StepResult`."""
    saved: dict[str, Any] = {}
    results: list[StepResult] = []
    for index, step in enumerate(steps, 1):
        tool = step["call"]
        start = time.monotonic()
        rules = step.get("expect") or {}
        try:
            args = resolve(step.get("args") or {}, saved, special)
        except KeyError as error:
            failed = StepResult(
                index, tool, bool(step.get("setup")), False, len(rules), 0.0,
                failures=[f"argument: {error.args[0]}"],
            )  # fmt: skip
            results.append(failed)
            if echo:
                echo(failed)
            break
        intent = step.get("intent") or f"Acceptance {label}, step {index}"
        try:
            outcome = await call_tool(
                session, tool, args, intent=intent, detail=step.get("detail") or "full"
            )
        except Exception as error:  # Transport failure: the scenario cannot continue.
            failed = StepResult(
                index, tool, bool(step.get("setup")), False, len(rules),
                time.monotonic() - start, failures=[f"call failed: {error!r}"],
            )  # fmt: skip
            results.append(failed)
            if echo:
                echo(failed)
            break
        failures = expectations.evaluate(outcome, rules)
        expects_error = any(p.startswith("error") for p in rules)
        if not outcome.get("ok") and not expects_error:
            error = outcome.get("error") or {}
            failures.insert(0, f"unexpected error {error.get('code')}: {error.get('message')}")
        result = StepResult(
            index,
            tool,
            bool(step.get("setup")),
            not failures,
            len(rules),
            time.monotonic() - start,
            _digest(outcome),
            failures,
            outcome,
        )
        results.append(result)
        if echo:
            echo(result)
        if step.get("save"):
            saved[step["save"]] = outcome
        if not outcome.get("ok") and not rules:
            break  # A failed setup step: later steps would only cascade.
    return results


# ---------------------------------------------------------------------------
# Live session plumbing


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def commit_sha(root: Path = ROOT) -> str | None:
    """HEAD's commit read from ``.git`` files (no git process)."""
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        loose = git / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip()
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    except OSError:
        return None
    return None


def source_digest(tool: str) -> str | None:
    """SHA-256 of the module that defines ``tool`` (evidence ``source_sha256``)."""
    spec = catalogue.load().get(tool)
    if spec is None:
        return None
    module = sys.modules.get(spec.handler.__module__)
    path = getattr(module, "__file__", None)
    return sha256(Path(path).read_bytes()) if path else None


class DevLock:
    """``scripts/dev_lock.py`` held by the runner for its whole run.

    ``release`` gives up only a lock this runner acquired: an agent that holds the
    lock (``dev_lock.py acquire --owner``) and runs ``call``/``run`` keeps it."""

    def __init__(self, workspace: Path, owner: str, *, wait: float = 1800, stale: float = 20):
        script = ROOT / "scripts" / "dev_lock.py"
        spec = importlib.util.spec_from_file_location("nx_mcp_dev_lock", script)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load {script}")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.path = self.module.lock_path(str(workspace))
        self.owner = owner
        self.wait = wait
        self.stale = stale * 60
        self.inherited: bool | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.inherited is None:  # the first acquire: was the lock already the owner's?
            current = self.module.read(self.path)
            self.inherited = bool(current and current.get("owner") == self.owner)
        deadline = time.monotonic() + self.wait
        while not self.module.try_acquire(self.path, self.owner, self.stale):
            if time.monotonic() > deadline:
                raise TimeoutError(f"dev lock held: {self.module.read(self.path)}")
            time.sleep(5)

    refresh = acquire

    def release(self) -> None:
        """Release the lock this runner acquired; a lock the owner already held before
        the run (``dev_lock.py acquire`` by the agent) stays theirs."""
        if self.inherited:
            return
        current = self.module.read(self.path)
        if current and current.get("owner") == self.owner:
            self.path.unlink(missing_ok=True)


def descriptor_path() -> Path | None:
    """The bridge descriptor the sidecar connects to (the interactive one by default)."""
    if os.environ.get("NX_MCP_BRIDGE_DESCRIPTOR"):
        return Path(os.environ["NX_MCP_BRIDGE_DESCRIPTOR"])
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "nx-mcp" / "interactive-bridge.json"
    return None


@contextlib.asynccontextmanager
async def sidecar(workspace: Path):
    """A stdio MCP client session of a fresh ``python -m nx_mcp.server``."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env["NX_MCP_WORKSPACE"] = str(workspace)
    env["NX_MCP_SURFACE"] = "agent"
    env["NX_MCP_DEV"] = "1"  # untested tools must run to earn their first evidence
    descriptor = descriptor_path()
    if descriptor is not None:
        env["NX_MCP_BRIDGE_DESCRIPTOR"] = str(descriptor)
    params = StdioServerParameters(command=sys.executable, args=["-m", "nx_mcp.server"], env=env)
    with open(os.devnull, "w") as quiet:
        async with (
            stdio_client(params, errlog=quiet) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session


async def nx_version(session) -> tuple[str, str]:
    """``(build key, build text)`` reported by the bridge."""
    status = await call_tool(session, "nx_get_status", {"include": ["bridge"]}, intent=None)
    if not status.get("ok"):
        raise RuntimeError(f"nx_get_status failed: {status.get('error')}")
    bridge = (status.get("data") or {}).get("bridge") or {}
    text = str(bridge.get("nx_build") or bridge.get("nx_version") or "")
    key = core_evidence.build_key(bridge.get("nx_version") or text)
    if key is None:
        raise RuntimeError(f"The bridge reports no NX version: {status}")
    return key, text


# ---------------------------------------------------------------------------
# Running scenarios


def _echo(result: StepResult) -> None:
    mark = "pass" if result.passed else "FAIL"
    kind = "setup" if result.setup else f"{result.checks} checks"
    print(f"  [{result.index:2}] {mark} {result.call} ({kind}, {result.seconds:.1f} s)")
    for failure in result.failures:
        print(f"         - {failure}")


async def run_scenario(session, path: Path, *, workspace: Path, run_id: str, build, owner) -> dict:
    data = load_scenario(path)
    domain = path.parent.name
    run_dir = workspace / "smoke" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    special = {"$run": str(run_dir)}
    if data.get("part"):
        # NX refuses a second loaded part with the same basename: make it unique per run.
        special["$part"] = str(run_dir / f"{domain}_{path.stem}_{run_id.split('-')[-1]}.prt")
    if data.get("fixture"):
        fixture = SCENARIOS / data["fixture"]
        target = run_dir / f"{domain}_{path.stem}_{fixture.name}"
        shutil.copyfile(fixture, target)
        special["$fixture"] = str(target)
    started = utc_now()
    before = await loaded_parts(session)
    # Only a scenario that changes the panel itself (nx_set_control) gets it restored:
    # any other change during a run is the engineer's and stays.
    controls = any(step.get("call") == "nx_set_control" for step in data["steps"])
    panel = find_dev_panel() if controls else None
    mode = await panel_mode(session) if panel is not None else None
    try:
        steps = await run_steps(
            session, data["steps"], special, label=f"{domain}/{path.stem}", echo=_echo
        )
    finally:
        # Keep NX fast: close what the scenario loaded, even when a step failed.
        cleanup = await close_new_parts(
            session, before, data.get("cleanup", "close"), own_dir=run_dir, workspace=workspace
        )
        if panel is not None and mode is not None:
            restored = await restore_panel_mode(session, panel, mode)
            if restored is not None:
                cleanup["panel"] = restored
                if not restored["restored"]:
                    cleanup["failed"].append(f"could not restore the panel mode {mode!r}")
    for message in cleanup.get("failed", []):
        print(f"  cleanup: {message}")
    finished = utc_now()
    checks = sum(s.checks for s in steps)
    passed = len(steps) == len(data["steps"]) and all(s.passed for s in steps)
    return {
        "format": 1,
        "scenario": relative(path),
        "scenario_sha256": sha256(path.read_bytes()),
        "domain": domain,
        "covers": data["covers"],
        "nx_version": build[0],
        "nx_build": build[1],
        "commit": commit_sha(),
        "sources": {tool: source_digest(tool) for tool in data["covers"]},
        "run_id": run_id,
        "owner": owner,
        "part": special.get("$part"),
        "started": started,
        "finished": finished,
        "passed": passed,
        "checks": checks,
        "failed_checks": sum(len(s.failures) for s in steps if s.checks),
        "steps": [s.record() for s in steps],
        "cleanup": cleanup,
    }


async def loaded_parts(session) -> list[dict] | None:
    """Loaded parts in load order (``None`` when NX cannot list them)."""
    rows: list[dict] = []
    offset: int | None = 0
    while offset is not None:
        args = {"offset": offset, "limit": 200}
        listed = await call_tool(session, "nx_list_parts", args, intent=None)
        if not listed.get("ok"):
            return None
        rows.extend((listed.get("data") or {}).get("items") or [])
        offset = (listed.get("page") or {}).get("next_offset")
    return rows


CLOSE_PASSES = 4  # close passes: assemblies, then the prototypes they released


def close_failure(path: str | None, error: dict | None) -> str:
    """``could not close <path>: <code>: <message>`` with NX's own code when known."""
    error = error or {}
    reason = ": ".join(str(x) for x in (error.get("code"), error.get("message")) if x)
    if error.get("nx_code") is not None:
        reason += f" (nx_code {error['nx_code']})"
    parents = (error.get("details") or {}).get("parent_assemblies")
    if parents:
        reason += f"; used by {', '.join(map(str, parents))}"
    return f"could not close {path}: {reason or 'no reason given'}"


def _inside(path: str | None, own_dir: Path | None, workspace: Path | None) -> bool:
    """Whether a listed part path (workspace-relative or absolute) is under ``own_dir``."""
    if not path or own_dir is None:
        return False
    full = Path(path)
    if not full.is_absolute() and workspace is not None:
        full = Path(workspace) / full
    try:
        full.resolve().relative_to(Path(own_dir).resolve())
    except ValueError:
        return False
    return True


async def close_new_parts(
    session,
    before: list[dict] | None,
    mode: str = "close",
    *,
    own_dir: Path | None = None,
    workspace: Path | None = None,
) -> dict:
    """Close every part loaded since ``before``; re-activate the earlier parts only when
    they are the run's own (under ``own_dir``) and still loaded (by fresh part IDs).

    ``mode`` is the scenario's ``cleanup``: ``close`` (default, without saving),
    ``save`` (save, then close) or ``keep``. Closing a drawing part closes its
    sheets. Assemblies go before their prototypes: a refused close is retried
    after the others, with fresh part IDs and the refusing parents first, for up
    to ``CLOSE_PASSES`` passes. A part still loaded is reported with NX's reason.
    Parts are closed by ID, never activated first. Another agent's or the
    engineer's display part is never displayed again: NX may stay without a display
    part, which is safe, while re-displaying a part this run did not make can
    change NX's application under its owner.
    """
    report: dict[str, Any] = {"mode": mode, "closed": [], "failed": []}
    if mode == "keep" or before is None:
        return report
    known = {p.get("path") for p in before}
    after = await loaded_parts(session) or []
    pending = [p for p in reversed(after) if p.get("path") not in known]
    reasons: dict[str, dict] = {}
    for _ in range(CLOSE_PASSES):
        if not pending:
            break
        refused = []
        live: dict | None = None  # the rows listed after the last close
        for part in pending:
            if live is not None:
                part = live.get(part.get("path"))
                if part is None:
                    continue  # NX unloaded it with its assembly
            closed = await call_tool(
                session,
                "nx_close_part",
                {"part": part["part"]["id"], "save": mode == "save"},
                intent="Acceptance cleanup: close a scenario part",
            )
            if closed.get("ok"):
                report["closed"].append(part.get("path"))
                reasons.pop(part.get("path"), None)
                # A close can unload prototypes and renew part IDs: list again.
                listed = await loaded_parts(session)
                live = None if listed is None else {p.get("path"): p for p in listed}
            else:
                refused.append(part)
                reasons[part.get("path")] = closed.get("error") or {}
        if not refused:
            pending = []
            break
        # Retry what is still loaded, with fresh rows.
        listed = await loaded_parts(session)
        if listed is None:
            pending = refused
            continue
        live = {p.get("path"): p for p in listed}
        pending = [live[p.get("path")] for p in refused if p.get("path") in live]
        parents = {
            parent
            for p in pending
            for parent in ((reasons.get(p.get("path")) or {}).get("details") or {}).get(
                "parent_assemblies"
            )
            or []
        }
        pending.sort(key=lambda p: p.get("path") not in parents)  # assemblies first
    report["failed"] = [close_failure(p.get("path"), reasons.get(p.get("path"))) for p in pending]
    display = next((p for p in before if p.get("display")), None)
    work = next((p for p in before if p.get("work")), None)
    if not _inside((display or {}).get("path"), own_dir, workspace):
        if display is not None:
            report["not_restored"] = display.get("path")
        return report
    if work is not None and not _inside(work.get("path"), own_dir, workspace):
        work = display
    targets = [(display, True)]
    if work is not None and work is not display:
        targets.append((work, False))  # a different work part, made work afterwards
    # Fresh part IDs: a close renews them, and a part the steps closed is not shown again.
    listed = await loaded_parts(session)
    current = None if listed is None else {p.get("path"): p for p in listed}
    for earlier, show in targets:
        if earlier is None:
            continue
        target: dict | None = earlier if current is None else current.get(earlier.get("path"))
        if target is None:
            continue  # the scenario closed it itself
        restored = await call_tool(
            session,
            "nx_activate_part",
            {"part": target["part"]["id"], "work": True, "display": show},
            intent="Acceptance cleanup: return to the previous part",
        )
        if restored.get("ok"):
            report.setdefault("restored", []).append(target.get("path"))
        else:
            error = restored.get("error") or {}
            reason = ": ".join(str(x) for x in (error.get("code"), error.get("message")) if x)
            report["failed"].append(f"could not re-activate {target.get('path')}: {reason}")
    return report


# The NX MCP panel's mode buttons (interactive.ID_MODES): a WM_COMMAND with one
# of these ids is the engineer's click.
PANEL_BUTTONS = {"auto": 111, "agent": 112, "ask": 113}
PANEL_RANK = {"ask": 0, "auto": 1, "agent": 2}  # interactive._RANK: control given to the agent
DEV_PANEL_TITLE = "NX MCP DEV"


def find_dev_panel() -> int | None:
    """The window handle of the development loop's NX MCP panel, else ``None``.

    Only the panel of ``dev_start_nx.py`` (title "NX MCP DEV ...") in the NX
    process of the bridge descriptor counts: production panels are never touched.
    """
    if sys.platform != "win32":
        return None
    descriptor = descriptor_path()
    try:
        pid = int(json.loads(descriptor.read_text(encoding="utf-8"))["pid"]) if descriptor else 0
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not pid:
        return None
    import ctypes
    from ctypes import wintypes as w

    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    found: list[int] = []

    def visit(hwnd, _):
        owner = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        name = ctypes.create_unicode_buffer(256)
        title = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, name, 256)
        user.GetWindowTextW(hwnd, title, 256)
        if name.value.startswith("NxMcpPanel") and title.value.startswith(DEV_PANEL_TITLE):
            found.append(int(hwnd))
            return False
        return True

    user.EnumWindows(callback_type(visit), 0)
    return found[0] if found else None


def press_panel(panel: int, mode: str) -> None:
    """Click the panel's mode button (posted: NX applies it on its next tick)."""
    import ctypes

    ctypes.windll.user32.PostMessageW(panel, 0x111, PANEL_BUTTONS[mode], 0)  # WM_COMMAND


async def panel_mode(session) -> str | None:
    """The panel mode (``nx_set_control`` status; never waits), ``None`` without a panel."""
    status = await call_tool(session, "nx_set_control", {"mode": "status"}, intent=None)
    mode = (status.get("data") or {}).get("mode") if status.get("ok") else None
    return mode if mode in PANEL_BUTTONS else None


async def restore_panel_mode(
    session, panel: int, mode: str, *, wait: float = 15.0, poll: float = 0.5
) -> dict | None:
    """Put the development panel back in ``mode`` when a scenario changed it.

    A scenario may leave NX in "Ask me first" (session/set_control.json): the agent
    cannot raise its own control, so the runner clicks the mode button on the
    development panel the way the engineer would. It only ever raises the mode back:
    a higher mode now is the engineer's choice. ``None`` when nothing was pressed.
    """
    current = await panel_mode(session)
    if current is None or PANEL_RANK[current] >= PANEL_RANK[mode]:
        return None  # Unchanged, or raised by the engineer: never lower their choice.
    report: dict[str, Any] = {"from": current, "to": mode, "restored": False}
    press_panel(panel, mode)
    deadline = time.monotonic() + wait
    while True:
        if await panel_mode(session) == mode:
            report["restored"] = True
            return report
        if time.monotonic() > deadline:
            return report
        await asyncio.sleep(poll)


def write_evidence(record: dict, root: Path = EVIDENCE) -> Path:
    """Passing runs replace ``<name>.json``; a failing run is kept beside it."""
    folder = root / record["nx_version"] / record["domain"]
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(record["scenario"]).stem
    target = folder / (name + (".json" if record["passed"] else ".last-failure.json"))
    target.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if record["passed"]:
        (folder / (name + ".last-failure.json")).unlink(missing_ok=True)
    return target


async def run(names, *, workspace: Path, owner: str, evidence_root: Path = EVIDENCE) -> int:
    paths = scenario_files(names)
    for path in paths:
        load_scenario(path)  # Format errors stop the run before NX is touched.
    lock = DevLock(workspace, owner)
    lock.acquire()
    failed = 0
    try:
        async with sidecar(workspace) as session:
            build = await nx_version(session)
            run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            print(f"NX {build[1]} ({build[0]}), run {run_id}, owner {owner}")
            for path in paths:
                lock.refresh()
                print(f"{relative(path)}")
                record = await run_scenario(
                    session, path, workspace=workspace, run_id=run_id, build=build, owner=owner
                )
                written = write_evidence(record, evidence_root)
                verdict = "PASS" if record["passed"] else "FAIL"
                print(f"  {verdict}: {record['checks']} checks -> {relative(written)}")
                failed += not record["passed"]
    finally:
        lock.release()
    return 1 if failed else 0


async def call(
    steps: list[dict], *, workspace: Path, owner: str, limit: int = 600, keep: bool = False
) -> int:
    """Ad-hoc steps through the same client (the live development loop); no evidence."""
    lock = DevLock(workspace, owner)
    lock.acquire()
    try:
        async with sidecar(workspace) as session:
            run_dir = workspace / "smoke" / owner
            run_dir.mkdir(parents=True, exist_ok=True)
            special = {"$run": str(run_dir)}
            normalized = [
                {
                    **{k: v for k, v in s.items() if k != "tool"},
                    "call": s.get("call", s.get("tool")),
                }
                for s in steps
            ]

            def echo(result: StepResult) -> None:
                text = json.dumps(result.result, ensure_ascii=False, default=str)
                text = text if len(text) <= limit else text[:limit] + " …"
                print(
                    f"[{result.index}] {'OK  ' if result.passed else 'FAIL'} {result.call} {text}"
                )
                for failure in result.failures:
                    print(f"     - {failure}")

            before = await loaded_parts(session)
            try:
                results = await run_steps(session, normalized, special, label="dev call", echo=echo)
            finally:
                cleanup = await close_new_parts(
                    session,
                    before,
                    "keep" if keep else "close",
                    own_dir=run_dir,
                    workspace=workspace,
                )
            if cleanup["closed"] or cleanup["failed"]:
                print(f"cleanup: closed {len(cleanup['closed'])}, problems {cleanup['failed']}")
    finally:
        lock.release()
    return 0 if results and all(r.passed for r in results) else 1


# ---------------------------------------------------------------------------
# Fold


def fold(
    evidence_root: Path = EVIDENCE, out_dir: Path = core_evidence.EVIDENCE_DIR
) -> tuple[dict[str, dict], list[str]]:
    """Rebuild ``out_dir/<domain>.json`` from passing evidence files only.

    Evidence whose scenario file changed since the run is stale and skipped.
    Returns ``({tool: {build: entry}}, problems)``.
    """
    problems: list[str] = []
    records = []
    for path in sorted(evidence_root.glob("*/*/*.json")):
        if path.name.endswith(".last-failure.json"):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            problems.append(f"{relative(path)}: not JSON")
            continue
        if not record.get("passed"):
            continue
        scenario = ROOT / record["scenario"]
        if not scenario.is_file() or sha256(scenario.read_bytes()) != record.get("scenario_sha256"):
            problems.append(f"{relative(path)}: scenario changed since the run; rerun it")
            continue
        records.append(record)
    with tempfile.TemporaryDirectory() as temp:
        for record in records:
            for tool, variants in sorted(record["covers"].items()):
                spec = catalogue.load().get(tool)
                if spec is None:
                    problems.append(f"{record['scenario']}: {tool} is not a catalogue tool")
                    continue
                core_evidence.record(
                    spec.domain,
                    tool,
                    record["nx_version"],
                    variants=list(variants),
                    scenario=record["scenario"],
                    scenario_sha256=record["scenario_sha256"],
                    source_sha256=(record.get("sources") or {}).get(tool) or "",
                    checks=record["checks"],
                    recorded=record["finished"],
                    directory=temp,
                )
        out_dir.mkdir(parents=True, exist_ok=True)
        written = {p.name for p in Path(temp).glob("*.json")}
        for stale in out_dir.glob("*.json"):
            if stale.name not in written:
                stale.unlink()
        for name in sorted(written):
            os.replace(Path(temp) / name, out_dir / name)
    if out_dir == core_evidence.EVIDENCE_DIR:
        core_evidence.reload()
    return core_evidence.load(out_dir), problems


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m nx_mcp.acceptance", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="replay scenarios against the live NX bridge")
    run_parser.add_argument("scenarios", nargs="*", help="domain, domain/name or file (all)")
    run_parser.add_argument(
        "--evidence", type=Path, default=EVIDENCE, help="evidence root (acceptance/evidence)"
    )
    call_parser = sub.add_parser("call", help="run ad-hoc steps (no evidence)")
    call_parser.add_argument("file", nargs="?", help="JSON list of steps")
    call_parser.add_argument("--inline", help="JSON list of steps")
    call_parser.add_argument("--limit", type=int, default=600, help="characters per result")
    call_parser.add_argument("--keep", action="store_true", help="leave new parts loaded")
    for p in (run_parser, call_parser):
        p.add_argument("--owner", required=True, help="dev_lock.py owner")
        p.add_argument("--workspace", default=os.environ.get("NX_MCP_WORKSPACE"))
    sub.add_parser("fold", help="build src/nx_mcp/evidence/<domain>.json from evidence files")
    args = parser.parse_args(argv)
    if args.command == "fold":
        folded, problems = fold()
        for tool, builds in sorted(folded.items()):
            for build, entry in sorted(builds.items()):
                print(
                    f"{tool} {build}: variants {entry.get('variants')} ({entry.get('checks')} checks)"
                )
        for problem in problems:
            print("PROBLEM:", problem)
        return 1 if problems else 0
    if not args.workspace:
        parser.error("set NX_MCP_WORKSPACE or pass --workspace")
    workspace = Path(args.workspace).resolve()
    if args.command == "run":
        return asyncio.run(
            run(
                args.scenarios,
                workspace=workspace,
                owner=args.owner,
                evidence_root=args.evidence.resolve(),
            )
        )
    if bool(args.file) == bool(args.inline):
        parser.error("pass a steps file or --inline")
    steps = json.loads(args.inline if args.inline else Path(args.file).read_text(encoding="utf-8"))
    return asyncio.run(
        call(steps, workspace=workspace, owner=args.owner, limit=args.limit, keep=args.keep)
    )

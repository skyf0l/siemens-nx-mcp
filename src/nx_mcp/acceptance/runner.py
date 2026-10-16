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
the tool as ``"$0.data.sketch"``, an ``nx_run_batch`` step reference). A step with
``"direct": true`` calls a listed core tool as its own MCP tool (compact result). A step with
``"call": "engineer"`` and ``"args": {"code": ...}`` plays the engineer: it runs the
Python snippet in NX through the bridge's development tool (``NX_MCP_DEV=1``; names as
``scripts/dev_python.py``; ``$run`` in the code is the run directory) and then lets the interactive host check its mark
fingerprint as it does before every agent call, so the edit becomes an engineer change
(an undo-history barrier) and never an agent operation. The part-ownership gate stays
on in development: an engineer step acts outside it, but a part it loads is the run's
own (the host tracks what a development call loads), so a scenario that needs an
engineer part removes it from ``services.owned_parts`` in an engineer step. ``expect``
values take ``$name...`` strings the same way (other ``$`` text such as ``$~C`` stays).
Every call goes through the stdio MCP client of a fresh
``python -m nx_mcp.server`` (agent profile): catalogue read tools through
``nx_invoke_read``, everything else through ``nx_invoke`` with an intent, both
with ``detail=full`` (a step's ``"detail": "compact"`` asks for the compact
projection, to check ``result_id`` and ``omitted``); ``expect`` paths address the
envelope (``ok``, ``data``, ``error.code``/``category``, ``mutation_outcome``). Only
steps with an ``expect`` count as checks.

``run`` and ``call`` hold ``scripts/dev_lock.py``'s lock for their whole duration
(:class:`DevLock`: acquired once, refreshed every 30 s, released at the end) and present
the caller id ``dev-<owner>`` to NX (:func:`caller_id`): parts one call with an
``--owner`` created are that owner's own in its next call or run.

A run writes ``acceptance/evidence/<nx-version>/<domain>/<name>.json`` with the
deterministic proof only (scenario and tool source digests, NX build, per-step
pass/fail and check counts, one ``recorded`` time): a file that differs from the
new record only in ``recorded`` is left untouched, so an unchanged re-run yields
no diff. Run details (run id, owner, part path, times, step seconds and result
digests, cleanup, commit) go to the local run log
``<workspace>/.nx-mcp/acceptance-runs/<run-id>.json``. ``fold`` builds
``src/nx_mcp/evidence/<domain>.json`` from passing evidence files only.

A failing run's ``<name>.last-failure.json`` says why in public terms only
(:func:`public_error`, :func:`public_failure`): error codes, categories and fields, the
missing NXOpen member of ``NX_VERSION_UNSUPPORTED``, check paths, operators and numbers;
never NX's message (it can name a licence feature or a part), a path or a name. A run NX
refused a licence in writes no evidence at all: it would tell which licences the seat
lacks (``fold`` first rewrites older failure records so, :func:`scrub_failure_records`).

``verify`` is ``run`` for a volunteer on another NX build: every scenario (or the given
domains), refused beside parts it did not create or when NX MCP only reads there, the
console in public terms, licence refusals counted as skipped; then a fold, the list of
``acceptance/evidence/<build>/**`` files to contribute, a summary and
``<workspace>/.nx-mcp/verify-last.json`` (counts for ``python -m nx_mcp.report``).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import datetime as _dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nx_mcp.acceptance import expect as expectations
from nx_mcp.core import catalogue
from nx_mcp.core import evidence as core_evidence
from nx_mcp.workspace import default_workspace

# The checkout (scenarios, fixtures, scripts/dev_lock.py); an installed release
# points NX_MCP_SOURCE_ROOT at its source/ folder.
ROOT = Path(os.environ.get("NX_MCP_SOURCE_ROOT") or Path(__file__).resolve().parents[3])
SCENARIOS = ROOT / "acceptance"
DEV_LOCK_SCRIPT = ROOT / "scripts" / "dev_lock.py"
EVIDENCE = SCENARIOS / "evidence"
STEP_KEYS = {"call", "args", "save", "expect", "setup", "intent", "detail", "direct"}
ENGINEER = "engineer"  # a step that edits NX as the engineer would (development only)
# Appended to an engineer step's snippet: the host's own check between agent calls.
_NOTICE = """
_host = services.host
if _host is not None:
    _host._engineer_had_nx = True
    _noticed = bool(_host._check_engineer_change())
else:
    services.revalidate()
    services.engineer_changed()
    _noticed = True
result = {"noticed": _noticed, "value": result}
"""
CLEANUP = ("close", "save", "keep")
EVIDENCE_FORMAT = 2  # 2: deterministic proof only; run details go to the run log
RUN_LOG = Path(".nx-mcp") / "acceptance-runs"  # under the workspace, never committed


class ScenarioError(ValueError):
    """A scenario file breaks the format."""


# ---------------------------------------------------------------------------
# Scenarios


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    """SHA-256 of a text file with LF line endings, so a Windows (CRLF) checkout
    and an LF one record the same evidence."""
    return sha256(Path(path).read_bytes().replace(b"\r\n", b"\n"))


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


_LABEL = re.compile(r"^\$[A-Za-z_]\w*(?:[./]|$)")


def resolve_expect(rules: Any, saved: dict, special: dict) -> Any:
    """``expect`` values with the substitutions of :func:`resolve` (``{"equals":
    "$link.data.part"}``). Only ``$name`` strings are substituted: other strings that
    start with ``$`` (NX text codes such as ``$~C``) are kept as written."""
    if isinstance(rules, str):
        if rules.startswith("$$") or _LABEL.match(rules):
            return resolve(rules, saved, special)
        return rules
    if isinstance(rules, dict):
        return {k: resolve_expect(v, saved, special) for k, v in rules.items()}
    if isinstance(rules, list):
        return [resolve_expect(v, saved, special) for v in rules]
    return rules


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

    def error(self) -> dict | None:
        """``{code, message}`` of the step's error envelope, else ``None``."""
        outcome = self.result if isinstance(self.result, dict) else {}
        problem = outcome.get("error")
        if outcome.get("ok", True) or not isinstance(problem, dict):
            return None
        return {"code": problem.get("code"), "message": problem.get("message")}

    def record(self) -> dict:
        """The run-log entry: everything, including timing and the result digest."""
        out: dict[str, Any] = {
            "index": self.index,
            "call": self.call,
            "setup": self.setup,
            "passed": self.passed,
            "checks": self.checks,
            "seconds": round(self.seconds, 3),
            "result_sha256": self.result_sha256,
        }
        error = None if self.passed else self.error()
        if error:
            out["error"] = error
        if self.failures:
            out["failures"] = self.failures
        return out

    def public_error(self) -> dict | None:
        """The step's error as evidence keeps it (:func:`public_error`), else ``None``."""
        outcome = self.result if isinstance(self.result, dict) else {}
        problem = outcome.get("error")
        if outcome.get("ok", True) or not isinstance(problem, dict):
            return None
        return public_error(problem)

    def proof(self, scrub: Callable[[str], str] = str) -> dict:
        """The evidence entry: only what a re-run reproduces, plus why a failed step
        failed in public terms only: the error's code, category and field, and each
        failed check's path, operator and numbers (:func:`public_failure`). NX's own
        message never goes there: it can name a licence feature, a part or a path."""
        out: dict[str, Any] = {
            "index": self.index,
            "call": self.call,
            "setup": self.setup,
            "passed": self.passed,
            "checks": self.checks,
        }
        if self.passed:
            return out
        error = self.public_error()
        if error:
            out["error"] = error
        if self.failures:
            out["failures"] = [public_failure(scrub(f)) for f in self.failures]
        return out


# ---------------------------------------------------------------------------
# What evidence and verify may say about a failure (privacy scrub)

LICENSE_CODE = "NX_LICENSE_UNAVAILABLE"
_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_CATEGORY = re.compile(r"[a-z][a-z_]{0,39}")
_FIELD = re.compile(r"[A-Za-z0-9_/#.\-]{0,120}")
_MEMBER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,120}")
_PATH = r"[A-Za-z0-9_.\-]{1,160}"
_NUMBER = r"[-+0-9.eE,\[\]]+"


def public_error(problem: dict) -> dict:
    """``{code, category, field}`` of an error envelope (``member`` too for
    ``NX_VERSION_UNSUPPORTED``: the NXOpen member this NX build lacks). Anything that
    is not a plain token (a message, a path, a part or licence name) is left out."""
    code = str(problem.get("code") or "")
    out: dict[str, Any] = {"code": code if _CODE.fullmatch(code) else "NX_ERROR"}
    for key, pattern in (("category", _CATEGORY), ("field", _FIELD)):
        value = problem.get(key)
        if isinstance(value, str) and value and pattern.fullmatch(value):
            out[key] = value
    details = problem.get("details")
    member = details.get("member") if isinstance(details, dict) else problem.get("member")
    unsupported = out["code"] == "NX_VERSION_UNSUPPORTED"
    if unsupported and isinstance(member, str) and _MEMBER.fullmatch(member):
        out["member"] = member
    return out


def _numbers(text: str, op: str = "") -> Any:
    """``text`` (a ``repr``) as a number or a list of numbers, else ``None``; for
    ``count`` the length of the listed value."""
    try:
        value = ast.literal_eval(text.strip())
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    if op == "count" and isinstance(value, (list, tuple, dict, str)):
        return len(value)

    def number(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v

    if number(value):
        return value
    if isinstance(value, (list, tuple)) and value and all(number(v) for v in value):
        return list(value)
    return None


def _checked(path: str, op: str, wanted: Any, actual: Any) -> str:
    if wanted is None or actual is None:
        return f"{path}: {op} failed"

    def text(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"))

    return f"{path}: {op} failed (wanted {text(wanted)}, actual {text(actual)})"


def public_failure(text: str) -> str:
    """A failure line as evidence keeps it: the error code, the check's path and operator
    and its numbers; never NX's message, a wanted or actual text, a path or a name.
    Idempotent: a public line comes back unchanged."""
    text = str(text)
    found = re.match(r"unexpected error ([A-Za-z0-9_]+)", text)
    if found:
        code = found.group(1)
        return f"unexpected error {code if _CODE.fullmatch(code) else 'NX_ERROR'}"
    found = re.match(r"(argument|expect): (\$[A-Za-z_][A-Za-z0-9_.]{0,160})", text)
    if found:
        return f"{found.group(1)}: {found.group(2).rstrip('.')} unresolved"
    found = re.match(r"call failed: ([A-Za-z_][A-Za-z0-9_]{0,60})", text)
    if found:
        return f"call failed: {found.group(1)}"
    found = re.match(rf"({_PATH}): missing\b", text)
    if found:
        return f"{found.group(1)}: missing"
    found = re.fullmatch(
        rf"({_PATH}): ([a-z_]+) failed(?: \(wanted ({_NUMBER}), actual ({_NUMBER})\))?", text
    )  # already public
    if found:
        path, op, wanted, actual = found.groups()
        return _checked(path, op, _numbers(wanted or ""), _numbers(actual or ""))
    found = re.fullmatch(rf"({_PATH}): ([a-z_]+) (.*) failed \(actual (.*)\)", text, re.DOTALL)
    if found:
        path, op, wanted, actual = found.groups()
        return _checked(path, op, _numbers(wanted), _numbers(actual, op))
    return "check failed"


def license_refused(record: dict) -> bool:
    """Whether NX refused a licence in this run: such a run is never recorded, since a
    failure record would tell which licences this seat lacks."""
    for step in record.get("steps") or []:
        if (step.get("error") or {}).get("code") == LICENSE_CODE:
            return True
        failures = step.get("failures") or []
        if any(str(f).startswith(f"unexpected error {LICENSE_CODE}") for f in failures):
            return True
    return False


def scrub_record(record: dict) -> dict | None:
    """``record`` (an evidence file) with its failed steps in public terms only
    (:func:`public_error`, :func:`public_failure`); ``None`` when NX refused a licence
    in it (the record must not be kept). Passing steps are unchanged."""
    if not record.get("passed") and license_refused(record):
        return None
    steps = []
    for step in record.get("steps") or []:
        if step.get("passed"):
            steps.append(step)
            continue
        clean = {k: step[k] for k in ("index", "call", "setup", "passed", "checks") if k in step}
        if isinstance(step.get("error"), dict):
            clean["error"] = public_error(step["error"])
        if step.get("failures"):
            clean["failures"] = [public_failure(f) for f in step["failures"]]
        steps.append(clean)
    return {**record, "steps": steps}


def scrub_failure_records(root: Path = EVIDENCE) -> list[str]:
    """Rewrite every ``*.last-failure.json`` under ``root`` in public terms; delete one
    where NX refused a licence. Returns what changed (paths relative to ``root``)."""
    changed: list[str] = []
    for path in sorted(Path(root).glob("*/*/*.last-failure.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        clean = scrub_record(record)
        name = path.relative_to(root).as_posix()
        if clean is None:
            path.unlink()
            changed.append(f"removed {name} (a licence refusal is never recorded)")
        elif clean != record:
            path.write_bytes(dump(clean))
            changed.append(f"scrubbed {name}")
    return changed


async def call_tool(
    session, tool: str, args: dict, *, intent: str | None, detail: str = "full", direct=False
) -> dict:
    """One call through the gateways (``detail=full`` by default); returns the unwrapped result.
    ``direct``: call a listed core tool as its own MCP tool, with ``args`` as they are
    (compact detail, no intent), as an agent calls it."""
    spec = catalogue.load().get(tool)
    if direct:
        response = await session.call_tool(tool, args)
    elif spec is not None and spec.effect == "read":
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


async def engineer_edit(args: dict) -> dict:
    """An ``engineer`` step: ``args["code"]`` runs in NX (``nx_dev_python``), then the host
    notices the change (``data.noticed``); an error envelope when NX refused."""
    from nx_mcp.bridge import DescriptorBridgeClient

    path = descriptor_path()
    code = args.get("code") if isinstance(args, dict) else None
    if path is None or not isinstance(code, str):
        return unwrap("an engineer step needs args.code and the bridge descriptor")
    client = DescriptorBridgeClient(str(path))
    try:
        snippet = "result = None\n" + code + _NOTICE
        # The engineer may act with no work part (open or create one): the probe guard's
        # opt-in, since the scenario's own code decides what it touches.
        result = await client.call("nx_dev_python", {"code": snippet, "allow_no_part": True})
    except Exception as error:
        problem = {"code": getattr(error, "code", "NX_ERROR"), "message": str(error)}
        return {"ok": False, "error": problem}
    noticed = "'noticed': True" in str(result.get("result"))
    return {"ok": True, "data": {"noticed": noticed, "stdout": result.get("stdout", "")}}


def engineer_args(args: Any, special: dict) -> Any:
    """An engineer snippet sees ``$run`` (inside its code) as the run directory."""
    code = args.get("code") if isinstance(args, dict) else None
    if not isinstance(code, str) or "$run" not in special:
        return args
    return {**args, "code": code.replace("$run", special["$run"])}


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
        where = "argument"
        try:
            args = resolve(step.get("args") or {}, saved, special)
            where = "expect"
            rules = resolve_expect(rules, saved, special)
        except KeyError as error:
            failed = StepResult(
                index, tool, bool(step.get("setup")), False, len(rules), 0.0,
                failures=[f"{where}: {error.args[0]}"],
            )  # fmt: skip
            results.append(failed)
            if echo:
                echo(failed)
            break
        intent = step.get("intent") or f"Acceptance {label}, step {index}"
        try:
            if tool == ENGINEER:
                outcome = await engineer_edit(engineer_args(args, special))
            else:
                outcome = await call_tool(
                    session,
                    tool,
                    args,
                    intent=intent,
                    detail=step.get("detail") or "full",
                    direct=bool(step.get("direct")),
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
            problem = outcome.get("error") or {}
            failures.insert(0, f"unexpected error {problem.get('code')}: {problem.get('message')}")
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


def scrubber(replacements: list[tuple[Any, str]]) -> Callable[[str], str]:
    """A function replacing local values (paths in either slash style, also with the
    doubled backslashes of a ``repr``; any case) by placeholders, so evidence never
    records an absolute path or a username. Longer values are replaced first."""
    rules: list[tuple[re.Pattern[str], str]] = []
    for value, token in sorted(replacements, key=lambda r: -len(str(r[0] or ""))):
        text = str(value or "").rstrip("\\/")
        if len(text) < 3:
            continue
        forms = {text, text.replace("\\", "/"), text.replace("/", "\\")}
        forms |= {f.replace("\\", "\\\\") for f in forms}
        pattern = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
        # A literal replacement: only a backslash is special in a re.sub template.
        rules.append((re.compile(pattern, re.IGNORECASE), token.replace("\\", "\\\\")))

    def scrub(text: str) -> str:
        for pattern, token in rules:
            text = pattern.sub(token, text)
        return text

    return scrub


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
    return file_sha256(Path(path)) if path else None


class LockLost(RuntimeError):
    """The dev lock this runner held went to someone else (taken over as stale)."""


class DevLock:
    """``scripts/dev_lock.py`` held by the runner for its whole run (all scenarios of a
    ``run``, all steps of a ``call``): acquired once, refreshed every ``beat`` seconds by
    a heartbeat thread, released at the end (also on an error or Ctrl+C; use it as a
    ``with`` block). It never changes hands in between: a lock found taken over raises
    :class:`LockLost` at the next :meth:`refresh` instead of being re-acquired.

    ``release`` gives up only a lock this runner acquired: an agent that holds the
    lock (``dev_lock.py acquire --owner``) and runs ``call``/``run`` keeps it."""

    def __init__(
        self,
        workspace: Path,
        owner: str,
        *,
        wait: float = 1800,
        stale_seconds: float | None = None,
        beat: float | None = None,
        echo: Callable[[str], None] | None = print,
    ):
        script = DEV_LOCK_SCRIPT
        spec = importlib.util.spec_from_file_location("nx_mcp_dev_lock", script)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load {script}")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.path = self.module.lock_path(str(workspace))
        self.owner = owner
        self.wait = wait
        self.stale = self.module.STALE_SECONDS if stale_seconds is None else stale_seconds
        self.beat = self.module.BEAT_SECONDS if beat is None else beat
        self.echo = echo or (lambda _text: None)
        self.inherited: bool | None = None
        self.held = False
        self.lost: str | None = None  # who holds it now, once the heartbeat lost it
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> None:
        """Wait for the lock (up to ``wait`` seconds), then keep it fresh; a second
        acquire only refreshes it."""
        if self.held:
            self.refresh()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = self.module.read(self.path)
        self.inherited = bool(current and current.get("owner") == self.owner)
        deadline = time.monotonic() + self.wait
        told = False
        while True:
            acquired, note = self.module.try_acquire(self.path, self.owner, self.stale)
            if acquired:
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"dev lock {self.module.describe(self.module.read(self.path))}")
            if not told:
                self.echo(
                    f"Waiting for the dev lock, {self.module.describe(self.module.read(self.path))}"
                )
                told = True
            time.sleep(min(self.module.POLL_SECONDS, max(self.wait, 0.05)))
        if note and note != "refreshed":
            self.echo(f"Dev lock: {note}")
        self.held, self.lost = True, None
        self._stop.clear()
        self._thread = threading.Thread(target=self._heartbeat, name="dev-lock", daemon=True)
        self._thread.start()

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.beat):
            try:
                if not self.module.refresh(self.path, self.owner):
                    self.lost = self.module.describe(self.module.read(self.path))
                    self.echo(f"Dev lock lost: {self.lost}")
                    return
            except Exception:  # a busy guard or a file error: try again next beat
                continue

    def refresh(self) -> None:
        """Refresh now; :class:`LockLost` when the lock is no longer this owner's."""
        if self.lost is None and self.held and self.module.refresh(self.path, self.owner):
            return
        self.lost = self.lost or self.module.describe(self.module.read(self.path))
        raise LockLost(f"the dev lock of {self.owner} was lost: {self.lost}")

    def release(self) -> None:
        """Stop refreshing and release the lock this runner acquired; a lock the owner
        already held before the run (``dev_lock.py acquire`` by the agent) stays theirs."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None
        held, self.held = self.held, False
        if held and not self.inherited and self.lost is None:
            self.module.release(self.path, self.owner)

    def __enter__(self) -> DevLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def caller_id(owner: str) -> str:
    """The caller id the sidecar of ``owner``'s runs presents to NX (``core/busy.py``):
    the same for every ``run`` and ``call`` with that ``--owner``, so the parts one call
    created are the owner's own in the next."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "", owner)[:48] or "anonymous"
    return "dev-" + slug


def descriptor_path() -> Path | None:
    """The bridge descriptor the sidecar connects to (the interactive one by default)."""
    if os.environ.get("NX_MCP_BRIDGE_DESCRIPTOR"):
        return Path(os.environ["NX_MCP_BRIDGE_DESCRIPTOR"])
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "nx-mcp" / "interactive-bridge.json"
    return None


def sidecar_args(caller: str | None = None) -> list[str]:
    """The sidecar's ``python`` arguments: ``nx_mcp.server``, or with a fixed caller id
    (:mod:`nx_mcp.acceptance.sidecar_launch`) when ``caller`` is given."""
    if caller is None:
        return ["-m", "nx_mcp.server"]
    return ["-m", "nx_mcp.acceptance.sidecar_launch", caller]


@contextlib.asynccontextmanager
async def sidecar(workspace: Path, caller: str | None = None, *, development: bool = True):
    """A stdio MCP client session of a fresh ``python -m nx_mcp.server``; ``caller``: the
    caller id it presents to NX (:func:`caller_id`), else its own random one.
    ``development`` (default): a development sidecar, which offers untested tools."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env["NX_MCP_WORKSPACE"] = str(workspace)
    env["NX_MCP_SURFACE"] = "agent"
    if development:
        env["NX_MCP_DEV"] = "1"  # untested tools must run to earn their first evidence
    else:
        env.pop("NX_MCP_DEV", None)
    descriptor = descriptor_path()
    if descriptor is not None:
        env["NX_MCP_BRIDGE_DESCRIPTOR"] = str(descriptor)
    params = StdioServerParameters(command=sys.executable, args=sidecar_args(caller), env=env)
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
    return build_of(status)


async def bridge_status(session) -> tuple[tuple[str, str], dict]:
    """``((build key, build text), bridge section)`` of ``nx_get_status``."""
    status = await call_tool(session, "nx_get_status", {"include": ["bridge"]}, intent=None)
    return build_of(status), (status.get("data") or {}).get("bridge") or {}


def build_of(status: dict) -> tuple[str, str]:
    """``(build key, build text)`` of an ``nx_get_status`` result."""
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


def _echo(result: StepResult, *, public: bool = False) -> None:
    mark = "pass" if result.passed else "FAIL"
    kind = "setup" if result.setup else f"{result.checks} checks"
    print(f"  [{result.index:2}] {mark} {result.call} ({kind}, {result.seconds:.1f} s)")
    for failure in result.failures:
        print(f"         - {public_failure(failure) if public else failure}")


def _public_echo(result: StepResult) -> None:
    _echo(result, public=True)


async def run_scenario(
    session, path: Path, *, workspace: Path, run_id: str, build, public: bool = False
) -> tuple[dict, dict]:
    """Run one scenario: ``(evidence, details)``, the committed proof and its run-log entry.
    ``public`` (``verify``): the console shows each failure in public terms only
    (:func:`public_failure`) and cleanup problems as a count."""
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
    try:
        steps = await run_steps(
            session,
            data["steps"],
            special,
            label=f"{domain}/{path.stem}",
            echo=_public_echo if public else _echo,
        )
    finally:
        # Keep NX fast: close what the scenario loaded, even when a step failed.
        cleanup = await close_new_parts(
            session, before, data.get("cleanup", "close"), own_dir=run_dir, workspace=workspace
        )
    if public and cleanup.get("failed"):
        print(f"  cleanup: {len(cleanup['failed'])} scenario part(s) could not be closed")
    for message in [] if public else cleanup.get("failed", []):
        print(f"  cleanup: {message}")
    finished = utc_now()
    passed = len(steps) == len(data["steps"]) and all(s.passed for s in steps)
    scrub = scrubber(
        [
            (run_dir, "$run"),
            (workspace, "$workspace"),
            (ROOT, "$source"),
            (Path.home(), "~"),
            (run_id, "$run_id"),
        ]
    )
    evidence = {
        "format": EVIDENCE_FORMAT,
        "scenario": relative(path),
        "scenario_sha256": file_sha256(path),
        "domain": domain,
        "covers": data["covers"],
        "nx_version": build[0],
        "nx_build": build[1],
        "sources": {tool: source_digest(tool) for tool in data["covers"]},
        "recorded": finished,
        "passed": passed,
        "checks": sum(s.checks for s in steps),
        "failed_checks": sum(len(s.failures) for s in steps if s.checks),
        "steps": [s.proof(scrub) for s in steps],
    }
    details = {
        "scenario": evidence["scenario"],
        "passed": passed,
        "part": special.get("$part"),
        "started": started,
        "finished": finished,
        "steps": [s.record() for s in steps],
        "cleanup": cleanup,
    }
    return evidence, details


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


def blocking_parts(
    rows: list[dict] | None, workspace: Path, *, beside_engineer: bool = False
) -> list[str]:
    """Paths of loaded parts a scenario must not start beside: the agent's own parts left
    loaded (gate 3t counts them, so steps without refs would need ``part``) and any part
    outside ``<workspace>/smoke/`` (the engineer's). ``beside_engineer`` (``run
    --beside-engineer``): the engineer's parts (not owned) do not block; rule G never
    counts them, and the run never closes, saves or displays a part it did not load.
    Unknown (``None``): none."""
    smoke = Path(workspace) / "smoke"
    return [
        str(row.get("path"))
        for row in rows or []
        if row.get("owned")
        or (not beside_engineer and not _inside(row.get("path"), smoke, workspace))
    ]


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
    """Close every part loaded since ``before`` under the run's own folder ``own_dir``;
    re-activate the earlier parts only when they are the run's own and still loaded (by
    fresh part IDs). A part loaded meanwhile anywhere else (another agent's or the
    engineer's, in the same NX) is never closed; without ``own_dir`` nothing is.

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
    pending = [
        p
        for p in reversed(after)
        if p.get("path") not in known and _inside(p.get("path"), own_dir, workspace)
    ]
    reasons: dict = {}  # path -> the error of the refused close
    for _ in range(CLOSE_PASSES):
        if not pending:
            break
        refused = []
        live: dict | None = None  # the rows listed after the last close
        for row in pending:
            part = live.get(row.get("path")) if live is not None else row
            if part is None:
                continue  # NX unloaded it with its assembly
            closed = await call_tool(
                session,
                "nx_close_part",
                # force: the scenario's own parts, never busy for its cleanup (core/busy.py)
                {"part": part["part"]["id"], "save": mode == "save", "force": True},
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


def dump(data: Any) -> bytes:
    """The written JSON form: two-space indent, UTF-8, LF line ends on every OS."""
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def same_proof(path: Path, record: dict) -> bool:
    """Whether ``path`` already holds ``record`` apart from its ``recorded`` time."""
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False

    def proof(data: Any) -> Any:
        return (
            {k: v for k, v in data.items() if k != "recorded"} if isinstance(data, dict) else data
        )

    return bool(proof(existing) == proof(record))


def write_evidence(record: dict, root: Path = EVIDENCE) -> tuple[Path | None, bool]:
    """Passing runs replace ``<name>.json``; a failing run is kept beside it, in public
    terms only (:func:`scrub_record`). A run NX refused a licence in is not written at
    all (``(None, False)``): a failure record would tell which licences this seat lacks.

    Write-if-changed: a file equal to ``record`` in everything but ``recorded`` stays
    untouched, so an unchanged re-run yields no diff. Returns ``(path, changed)``.
    """
    clean = scrub_record(record)
    if clean is None:
        return None, False
    record = clean
    folder = root / record["nx_version"] / record["domain"]
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(record["scenario"]).stem
    target = folder / (name + (".json" if record["passed"] else ".last-failure.json"))
    changed = not same_proof(target, record)
    if changed:
        temp = target.with_name(target.name + ".tmp")
        temp.write_bytes(dump(record))
        os.replace(temp, target)
    if record["passed"]:
        (folder / (name + ".last-failure.json")).unlink(missing_ok=True)
    return target, changed


def write_run_log(workspace: Path, log: dict) -> Path:
    """Write the local run log ``<workspace>/.nx-mcp/acceptance-runs/<run-id>.json``."""
    path = Path(workspace) / RUN_LOG / f"{log['run_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(dump(log))
    os.replace(temp, path)
    return path


async def run(
    names,
    *,
    workspace: Path,
    owner: str,
    evidence_root: Path = EVIDENCE,
    beside_engineer: bool = False,
) -> int:
    paths = scenario_files(names)
    for path in paths:
        load_scenario(path)  # Format errors stop the run before NX is touched.
    failed = 0
    with DevLock(workspace, owner) as lock:  # one lock for all scenarios, kept fresh
        async with sidecar(workspace, caller_id(owner)) as session:
            build = await nx_version(session)
            run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            print(f"NX {build[1]} ({build[0]}), run {run_id}, owner {owner}")
            log: dict[str, Any] = {
                "run_id": run_id,
                "owner": owner,
                "commit": commit_sha(),
                "nx_version": build[0],
                "nx_build": build[1],
                "workspace": str(workspace),
                "started": utc_now(),
                "finished": None,
                "scenarios": [],
            }
            for path in paths:
                try:
                    lock.refresh()
                except LockLost as error:
                    print(f"ABORTED before {relative(path)}: {error}")
                    failed += 1
                    break
                print(f"{relative(path)}")
                blocking = blocking_parts(
                    await loaded_parts(session), workspace, beside_engineer=beside_engineer
                )
                if blocking:
                    print(
                        "  REFUSED: close these loaded parts before running scenarios "
                        f"(the agent's or the engineer's): {', '.join(blocking[:10])}"
                    )
                    failed += 1
                    break
                record, details = await run_scenario(
                    session, path, workspace=workspace, run_id=run_id, build=build
                )
                written, changed = write_evidence(record, evidence_root)
                where = relative(written) if written else None
                details.update(evidence=where, evidence_changed=changed)
                log["scenarios"].append(details)
                write_run_log(workspace, log)  # after each scenario: a crash keeps the rest
                verdict = "PASS" if record["passed"] else "FAIL"
                if written is None:
                    print(f"  {verdict}: NX refused a licence; no evidence written")
                else:
                    note = "" if changed else " (unchanged)"
                    print(f"  {verdict}: {record['checks']} checks -> {where}{note}")
                failed += not record["passed"]
            log["finished"] = utc_now()
            print(f"Run log: {write_run_log(workspace, log)}")
    return 1 if failed else 0


async def call(
    steps: list[dict], *, workspace: Path, owner: str, limit: int = 600, keep: bool = False
) -> int:
    """Ad-hoc steps through the same client (the live development loop); no evidence."""
    results: list[StepResult] = []
    with DevLock(workspace, owner) as lock:  # one lock for all steps, kept fresh
        async with sidecar(workspace, caller_id(owner)) as session:
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
        try:
            lock.refresh()
        except LockLost as error:
            print(f"WARNING: {error}")
            return 1
    return 0 if results and all(r.passed for r in results) else 1


# ---------------------------------------------------------------------------
# Verify: a volunteer's run on another NX build

VERIFY_SUMMARY = Path(".nx-mcp") / "verify-last.json"  # under the workspace (nx_mcp.report)


def needs_engineer(path: Path) -> bool:
    """Whether a scenario plays the engineer (``nx_dev_python``: NX with NX_MCP_DEV=1)."""
    return any(step["call"] == ENGINEER for step in load_scenario(path)["steps"])


async def engineer_steps_available() -> bool:
    """Whether NX runs development snippets (started with ``NX_MCP_DEV=1``): a snippet
    that touches nothing."""
    from nx_mcp.bridge import DescriptorBridgeClient

    path = descriptor_path()
    if path is None:
        return False
    try:
        client = DescriptorBridgeClient(str(path))
        await client.call("nx_dev_python", {"code": "result = None", "allow_no_part": True})
    except Exception:
        return False
    return True


def _failure_summary(record: dict) -> dict:
    """``{scenario, codes, members}`` of a failed run, in public terms only."""
    codes: set[str] = set()
    members: set[str] = set()
    for step in record.get("steps") or []:
        error = step.get("error") or {}
        if error.get("code"):
            codes.add(str(error["code"]))
        if error.get("member"):
            members.add(str(error["member"]))
    scenario = Path(record["scenario"])
    out: dict[str, Any] = {
        "scenario": f"{scenario.parent.name}/{scenario.stem}",
        "codes": sorted(codes),
    }
    if members:
        out["members"] = sorted(members)
    return out


def write_verify_summary(workspace: Path, summary: dict) -> Path:
    """``<workspace>/.nx-mcp/verify-last.json``: the last verify's counts (field report)."""
    path = Path(workspace) / VERIFY_SUMMARY
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(dump(summary))
    os.replace(temp, path)
    return path


def read_verify_summary(workspace: Path) -> dict | None:
    try:
        data = json.loads((Path(workspace) / VERIFY_SUMMARY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def verify(
    names,
    *,
    workspace: Path,
    owner: str,
    evidence_root: Path = EVIDENCE,
    out_dir: Path = core_evidence.EVIDENCE_DIR,
) -> int:
    """``verify``: every scenario (or those of ``names``) on the connected NX build, for a
    volunteer on another NX build. Refuses to start beside parts it did not create (no
    ``--beside-engineer``) or when NX MCP only reads there; holds the dev lock for the
    whole run; writes evidence for the build in public terms only (a run NX refused a
    licence in is skipped and never written), folds it, and prints the evidence files to
    contribute and a summary. 0: all ran passed; 1: a scenario failed; 2: refused."""
    paths = scenario_files(names)
    for path in paths:
        load_scenario(path)  # Format errors stop the run before NX is touched.
    counts = {"passed": 0, "failed": 0, "license": 0, "development": 0}
    failures: list[dict] = []
    with DevLock(workspace, owner) as lock:
        async with sidecar(workspace, caller_id(owner)) as session:
            build, bridge = await bridge_status(session)
            tested = bool(bridge.get("build_tested"))
            print(f"NX {build[1]} ({build[0]}): {'tested' if tested else 'untested'} NX build")
            if bridge.get("read_only"):
                print(
                    "REFUSED: NX MCP runs only read and view tools on this NX "
                    "(NX_MCP_UNTESTED=read_only, or NX's undo self-test failed). Unset "
                    "NX_MCP_UNTESTED for NX and restart it; if the self-test failed, send "
                    "the field report instead (python -m nx_mcp.report)."
                )
                return 2
            blocking = blocking_parts(await loaded_parts(session), workspace)
            if blocking:
                print(
                    f"REFUSED: {len(blocking)} part(s) are loaded in NX that verify did not "
                    "create. Save and close every part in NX (or start NX again), then "
                    "run verify again."
                )
                return 2
            engineer_ok = True
            if any(needs_engineer(p) for p in paths):
                engineer_ok = await engineer_steps_available()
                if not engineer_ok:
                    print(
                        "Note: scenarios that play the engineer need NX started with "
                        "NX_MCP_DEV=1; they are skipped."
                    )
            run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            log: dict[str, Any] = {
                "run_id": run_id,
                "owner": owner,
                "command": "verify",
                "commit": commit_sha(),
                "nx_version": build[0],
                "nx_build": build[1],
                "started": utc_now(),
                "finished": None,
                "scenarios": [],
            }
            aborted = False
            for path in paths:
                try:
                    lock.refresh()
                except LockLost as error:
                    print(f"ABORTED before {relative(path)}: {error}")
                    aborted = True
                    break
                print(f"{relative(path)}")
                if not engineer_ok and needs_engineer(path):
                    counts["development"] += 1
                    print("  SKIPPED: needs NX started with NX_MCP_DEV=1")
                    continue
                if blocking_parts(await loaded_parts(session), workspace):
                    print("  ABORTED: a part verify did not create was loaded meanwhile")
                    aborted = True
                    break
                record, details = await run_scenario(
                    session, path, workspace=workspace, run_id=run_id, build=build, public=True
                )
                written, _ = write_evidence(record, evidence_root)
                details.update(evidence=relative(written) if written else None)
                log["scenarios"].append(details)
                write_run_log(workspace, log)  # after each scenario: a crash keeps the rest
                if record["passed"]:
                    counts["passed"] += 1
                    print(f"  PASS: {record['checks']} checks")
                elif written is None:
                    counts["license"] += 1
                    print("  SKIPPED: NX refused a licence (nothing recorded)")
                else:
                    counts["failed"] += 1
                    failures.append(_failure_summary(scrub_record(record) or record))
                    print(f"  FAIL: {', '.join(failures[-1]['codes']) or 'checks failed'}")
            log["finished"] = utc_now()
            write_run_log(workspace, log)
    _, problems = fold(evidence_root, out_dir)
    # Only what this run wrote: the files to include in a contribution.
    files = sorted({str(d["evidence"]) for d in log.get("scenarios", []) if d.get("evidence")})
    skipped = counts["license"] + counts["development"]
    summary = {
        "format": 1,
        "nx_version": build[0],
        "nx_build": build[1],
        "build_tested": tested,
        "finished": utc_now(),
        "complete": not aborted,
        "scenarios": len(paths),
        "passed": counts["passed"],
        "failed": counts["failed"],
        "skipped": skipped,
        "failed_scenarios": failures,
    }
    write_verify_summary(workspace, summary)
    if problems:
        print(f"Fold: {len(problems)} evidence file(s) of a changed scenario were left out")
    print(f"Files to include in a contribution ({len(files)}):")
    for name in files:
        print(f"  {name}")
    print(
        f"Summary: {counts['passed']} passed, {counts['failed']} failed, {skipped} skipped "
        f"({counts['license']} NX refused a licence, {counts['development']} need "
        f"NX_MCP_DEV=1) of {len(paths)} scenarios on NX {build[1]}"
        + ("; ABORTED before the end" if aborted else "")
    )
    return 1 if counts["failed"] or aborted else 0


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
        if not scenario.is_file() or file_sha256(scenario) != record.get("scenario_sha256"):
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
                    recorded=record["recorded"],
                    directory=temp,
                    nx_build=record.get("nx_build"),
                )
        out_dir.mkdir(parents=True, exist_ok=True)
        written = {p.name for p in Path(temp).glob("*.json")}
        for stale in out_dir.glob("*.json"):
            if stale.name not in written:
                stale.unlink()
        for name in sorted(written):
            fresh, target = Path(temp) / name, out_dir / name
            if not target.is_file() or target.read_bytes() != fresh.read_bytes():
                os.replace(fresh, target)  # unchanged files are left untouched
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
    run_parser.add_argument(
        "--beside-engineer",
        action="store_true",
        help="run while the engineer's own parts are loaded (never touched)",
    )
    verify_parser = sub.add_parser(
        "verify",
        help="run every scenario (or the given domains) on this NX build for a contribution",
    )
    verify_parser.add_argument("scenarios", nargs="*", help="domain, domain/name or file (all)")
    verify_parser.add_argument(
        "--evidence", type=Path, default=EVIDENCE, help="evidence root (acceptance/evidence)"
    )
    call_parser = sub.add_parser("call", help="run ad-hoc steps (no evidence)")
    call_parser.add_argument("file", nargs="?", help="JSON list of steps")
    call_parser.add_argument("--inline", help="JSON list of steps")
    call_parser.add_argument("--limit", type=int, default=600, help="characters per result")
    call_parser.add_argument("--keep", action="store_true", help="leave new parts loaded")
    for p in (run_parser, verify_parser, call_parser):
        p.add_argument(
            "--owner", required=True, help="dev_lock.py owner, also the caller identity in NX"
        )
        p.add_argument("--workspace", default=str(default_workspace()))
    sub.add_parser("fold", help="build src/nx_mcp/evidence/<domain>.json from evidence files")
    args = parser.parse_args(argv)
    if args.command == "fold":
        for change in scrub_failure_records(EVIDENCE):
            print(change)
        folded, problems = fold()
        for tool, builds in sorted(folded.items()):
            for build, entry in sorted(builds.items()):
                print(
                    f"{tool} {build}: variants {entry.get('variants')} ({entry.get('checks')} checks)"
                )
        for problem in problems:
            print("PROBLEM:", problem)
        return 1 if problems else 0
    workspace = Path(args.workspace).resolve()
    if args.command == "run":
        return asyncio.run(
            run(
                args.scenarios,
                workspace=workspace,
                owner=args.owner,
                evidence_root=args.evidence.resolve(),
                beside_engineer=args.beside_engineer,
            )
        )
    if args.command == "verify":
        return asyncio.run(
            verify(
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

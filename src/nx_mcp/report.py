"""Field report and capabilities page for an NX build NX MCP was not tested on.

``python -m nx_mcp.report`` writes ``<workspace>/nx-mcp-report-<build>-<date>.json``
for the engineer to check and send to the maintainer: the NX version text, the NX MCP,
Python and OS versions, the tools that failed on this NX build because NX lacks an
NXOpen call they need (``nx_get_status`` ``bridge.version_unsupported``: tool, variant,
member, exception type) and the counts of the last ``python -m nx_mcp.acceptance
verify``. It never holds a licence name or refusal, an NX error code or message, an
environment variable, a part name, a path (the workspace reads ``<workspace>``) or a
user or host name: every value is a plain token checked here.

``python -m nx_mcp.report --capabilities`` writes ``<workspace>/.nx-mcp/capabilities.html``
for the engineer only: per domain, the tools tested on this NX build, offered untested
(tested on another build), not supported here (failed this NX session) and refused for
a licence this NX session. It is local: it is never committed nor sent.

Both ask NX through the normal path: a sidecar (``python -m nx_mcp.server``) and
``nx_get_status``; the listing follows the evidence rules the sidecar applies
(``core/evidence.py``).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import getpass
import html
import json
import platform
import re
import socket
import sys
from pathlib import Path
from typing import Any

from nx_mcp.workspace import default_workspace

REPORT_FORMAT = 1
CAPABILITIES = Path(".nx-mcp") / "capabilities.html"
SEND = "check it, then send it to the maintainer"
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,80}")
_MEMBER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,120}")
_TEXT = re.compile(r"[A-Za-z0-9 ._()+-]{1,60}")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_SCENARIO = re.compile(r"[a-z_]{1,40}/[A-Za-z0-9_.-]{1,80}")
EXCEPTION_KEYS = ("exception", "exception_type", "error_type")


def _token(value: Any, pattern: re.Pattern[str] = _TOKEN) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


# ---------------------------------------------------------------------------
# NX status through a sidecar


async def _status(workspace: Path) -> dict | None:
    from nx_mcp.acceptance import runner

    async with runner.sidecar(workspace, development=False) as session:
        status = await runner.call_tool(
            session, "nx_get_status", {"include": ["bridge"]}, intent=None
        )
    return status if status.get("ok") else None


def nx_status(workspace: Path) -> dict | None:
    """The ``nx_get_status`` result (bridge section), or None when it cannot be read."""
    try:
        return asyncio.run(_status(workspace))
    except Exception:
        return None


def bridge_of(status: dict | None) -> dict:
    data = (status or {}).get("data") if isinstance(status, dict) else None
    bridge = (data or {}).get("bridge") if isinstance(data, dict) else None
    return bridge if isinstance(bridge, dict) else {}


def connected(bridge: dict) -> bool:
    return bridge.get("answered_from") in ("nx", "snapshot") or bridge.get("state") == "connected"


# ---------------------------------------------------------------------------
# Field report


def version_unsupported_rows(bridge: dict) -> list[dict]:
    """``{tool, variant, member, exception}`` of every tool this NX session found
    unsupported; any other field (its message, the build, the time) is left out."""
    rows = []
    for row in bridge.get("version_unsupported") or []:
        if not isinstance(row, dict) or not _token(row.get("tool")):
            continue
        exception = next((row[k] for k in EXCEPTION_KEYS if _token(row.get(k))), None)
        rows.append(
            {
                "tool": row["tool"],
                "variant": _token(row.get("variant")),
                "member": _token(row.get("member"), _MEMBER),
                "exception": exception,  # None: NX had none of the names the tool tries
            }
        )
    return rows


def verify_summary(summary: dict | None) -> dict | None:
    """The last ``verify``'s counts and failed scenarios (codes and NXOpen members)."""
    if not isinstance(summary, dict):
        return None
    failed = []
    for row in summary.get("failed_scenarios") or []:
        scenario = _token((row or {}).get("scenario"), _SCENARIO)
        if not scenario:
            continue
        entry: dict[str, Any] = {
            "scenario": scenario,
            "codes": sorted(c for c in row.get("codes") or [] if _token(c, _CODE)),
        }
        members = sorted(m for m in row.get("members") or [] if _token(m, _MEMBER))
        if members:
            entry["members"] = members
        failed.append(entry)
    finished = _token(summary.get("finished"), re.compile(r"[0-9TZ:-]{10,20}"))
    return {
        "nx_version": _token(summary.get("nx_version"), re.compile(r"v\d{4}")),
        "nx_build": _token(summary.get("nx_build"), _TEXT),
        "finished": finished,
        "complete": bool(summary.get("complete")),
        "scenarios": _count(summary.get("scenarios")),
        "passed": _count(summary.get("passed")),
        "failed": _count(summary.get("failed")),
        "skipped": _count(summary.get("skipped")),
        "failed_scenarios": failed,
    }


def field_report(status: dict | None, verify: dict | None, *, today: str | None = None) -> dict:
    """The report's content (see the module notes)."""
    from nx_mcp import __version__
    from nx_mcp.core.evidence import build_key

    bridge = bridge_of(status)
    text = _token(str(bridge.get("nx_build") or bridge.get("nx_version") or ""), _TEXT)
    self_test = bridge.get("self_test") if isinstance(bridge.get("self_test"), dict) else {}
    return {
        "format": REPORT_FORMAT,
        "created": today or _dt.date.today().isoformat(),
        "nx": {
            "connected": connected(bridge),
            "version": text,
            "build": build_key(text),
            "build_tested": bool(bridge.get("build_tested")),
            "self_test": _token((self_test or {}).get("state")),
            "read_only": bool(bridge.get("read_only")),
        },
        "nx_mcp": {"version": __version__},
        "python": platform.python_version(),
        "os": {
            "system": _token(platform.system(), _TEXT),
            "release": _token(platform.release(), _TEXT),
            "version": _token(platform.version(), _TEXT),
            "machine": _token(platform.machine(), _TEXT),
        },
        "version_unsupported": version_unsupported_rows(bridge),
        "verify": verify_summary(verify),
    }


def _private(workspace: Path) -> list[tuple[str, str]]:
    """Local values a report must never hold, with their placeholders."""
    values = [(str(workspace), "<workspace>"), (str(Path.home()), "~")]
    for read, token in ((getpass.getuser, "<user>"), (socket.gethostname, "<host>")):
        try:
            values.append((str(read()), token))
        except Exception:
            continue
    return values


def scrub_text(text: str, workspace: Path) -> str:
    """Replace any local path, user or host name left in ``text`` (the values are
    already checked tokens; this is a second line of defence)."""
    for value, token in _private(workspace):
        value = value.rstrip("\\/")
        if len(value) < 3:
            continue
        if token in ("<user>", "<host>"):
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])"
            text = re.sub(pattern, token, text, flags=re.IGNORECASE)
            continue
        for form in {value, value.replace("\\", "/"), value.replace("\\", "\\\\")}:
            text = re.sub(re.escape(form), token.replace("\\", "\\\\"), text, flags=re.I)
    return text


def write_report(workspace: Path, report: dict) -> Path:
    build = report["nx"]["build"] or "unknown"
    path = Path(workspace) / f"nx-mcp-report-{build}-{report['created']}.json"
    text = scrub_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


# ---------------------------------------------------------------------------
# Capabilities page (engineer-local)

COLUMNS = (
    ("tested", "Tested here"),
    ("untested", "Offered, untested"),
    ("unsupported", "Not supported here"),
    ("license", "Licence refused (this NX session)"),
)


def capabilities(bridge: dict) -> dict[str, list[dict]]:
    """``{domain: [{tool, label, tested, untested, unsupported, license}]}``: each column
    lists the variants (``["*"]`` for a tool without variants). Tools offered nowhere on
    this build (tested on no NX build) are left out."""
    from nx_mcp.core import catalogue, evidence

    build = evidence.build_key(str(bridge.get("nx_build") or bridge.get("nx_version") or ""))
    read_only = bool(bridge.get("read_only"))
    versions = [r for r in bridge.get("version_unsupported") or [] if isinstance(r, dict)]
    licences = [r for r in bridge.get("license_unavailable") or [] if isinstance(r, dict)]
    out: dict[str, list[dict]] = {}
    for spec in sorted(catalogue.load().values(), key=lambda s: (s.domain, s.name)):
        units: tuple = spec.variant_values() if isinstance(spec.variants, str) else ()
        row: dict[str, Any] = {"tool": spec.name, "label": spec.label}
        row.update({key: [] for key, _ in COLUMNS})
        for variant in units or (None,):
            name = variant or "*"
            if evidence.remembered(versions, spec.name, variant):
                row["unsupported"].append(name)
            elif evidence.remembered(licences, spec.name, variant):
                row["license"].append(name)
            elif evidence.available(spec, build, variant):
                row["tested"].append(name)
            elif evidence.offered(spec, build, variant, read_only=read_only):
                row["untested"].append(name)
        if any(row[key] for key, _ in COLUMNS):
            out.setdefault(spec.domain, []).append(row)
    return out


_STYLE = """
:root { --bg: #ffffff; --fg: #1d2330; --muted: #5d6675; --line: #d9dee7; --head: #f2f4f8;
        --ok: #1f7a3d; --warn: #9a6700; --bad: #b42318; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #15181e; --fg: #e6e9ef; --muted: #9aa3b2; --line: #2d333d; --head: #1e232b;
          --ok: #5fcf86; --warn: #e3b341; --bad: #ff7b72; }
}
body { background: var(--bg); color: var(--fg); margin: 0; padding: 16px;
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 24px 0 8px; }
p.note { color: var(--muted); margin: 0 0 12px; }
.table { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left;
         vertical-align: top; }
th { background: var(--head); font-weight: 600; }
td.tool code { font-size: 13px; } td.label { color: var(--muted); }
td.tested { color: var(--ok); } td.untested { color: var(--warn); }
td.unsupported, td.license { color: var(--bad); }
"""


def capabilities_page(bridge: dict, table: dict[str, list[dict]], *, now: str) -> str:
    text = str(bridge.get("nx_build") or bridge.get("nx_version") or "unknown")
    tested = "tested" if bridge.get("build_tested") else "not tested"
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>NX MCP capabilities</title>",
        f"<style>{_STYLE}</style></head><body><main>",
        f"<h1>NX MCP on NX {html.escape(text)}</h1>",
        f'<p class="note">This NX build is {tested} with NX MCP. Written {html.escape(now)}; '
        "the last two columns are this NX session's memory. This page stays on this "
        "computer: it is not part of the field report.</p>",
    ]
    for domain, rows in table.items():
        counts = {key: sum(1 for r in rows if r[key]) for key, _ in COLUMNS}
        parts.append(f"<h2>{html.escape(domain)}</h2>")
        parts.append(
            '<p class="note">'
            + ", ".join(f"{counts[key]} {title.lower()}" for key, title in COLUMNS)
            + "</p>"
        )
        parts.append('<div class="table"><table><thead><tr><th>Tool</th><th>Label</th>')
        parts.extend(f"<th>{html.escape(title)}</th>" for _, title in COLUMNS)
        parts.append("</tr></thead><tbody>")
        for row in rows:
            cells = [
                f'<td class="tool"><code>{html.escape(row["tool"])}</code></td>',
                f'<td class="label">{html.escape(row["label"])}</td>',
            ]
            for key, _ in COLUMNS:
                values = ["all" if v == "*" else v for v in row[key]]
                cells.append(f'<td class="{key}">{html.escape(", ".join(values))}</td>')
            parts.append("<tr>" + "".join(cells) + "</tr>")
        parts.append("</tbody></table></div>")
    parts.append("</main></body></html>\n")
    return "\n".join(parts)


def write_capabilities(workspace: Path, bridge: dict, *, now: str | None = None) -> Path:
    stamp = now or _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    page = capabilities_page(bridge, capabilities(bridge), now=stamp)
    path = Path(workspace) / CAPABILITIES
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Command line


def main(argv: list[str] | None = None, *, status_reader=nx_status) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m nx_mcp.report", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--capabilities",
        action="store_true",
        help="write <workspace>/.nx-mcp/capabilities.html (for you only) instead",
    )
    parser.add_argument("--workspace", default=str(default_workspace()))
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).resolve()
    status = status_reader(workspace)
    bridge = bridge_of(status)
    if args.capabilities:
        if not connected(bridge):
            print("NX is not connected: start NX with the NX MCP add-on, then run this again.")
            return 1
        print(f"Capabilities page: {write_capabilities(workspace, bridge)}")
        return 0
    from nx_mcp.acceptance.runner import read_verify_summary

    if not connected(bridge):
        print("NX is not connected: the report holds no NX information.")
    path = write_report(workspace, field_report(status, read_verify_summary(workspace)))
    print(f"Field report written to {path}")
    print(SEND.capitalize() + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())

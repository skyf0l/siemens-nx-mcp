"""Run MCP tool calls against the live NX bridge through a fresh sidecar.

Development helper for the live loop (see CLAUDE.md). Each step is a JSON object
{"tool": name, "args": {...}, "intent": optional note, "save": optional label};
a later step can reference an earlier result with "$label.json.pointer", e.g.
"$sketch.sketch.id" or "$ext.bodies.0.id". Steps run in order in one session;
the run stops at the first error unless the step has "expect_error": true.

    python scripts/dev_call.py steps.json
    python scripts/dev_call.py --inline '[{"tool": "nx_status"}]'
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def resolve(value, saved):
    if isinstance(value, str) and value.startswith("$"):
        label, *path = value[1:].split(".")
        current = saved[label]
        for key in path:
            current = current[int(key)] if isinstance(current, list) else current[key]
        return current
    if isinstance(value, dict):
        return {k: resolve(v, saved) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, saved) for v in value]
    return value


def short(payload, limit):
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + " …"


async def run(steps, limit):
    env = dict(os.environ)
    env.setdefault(
        "NX_MCP_BRIDGE_DESCRIPTOR",
        str(Path(os.environ["LOCALAPPDATA"]) / "nx-mcp" / "interactive-bridge.json"),
    )
    env["NX_MCP_SURFACE"] = "agent"
    params = StdioServerParameters(command=sys.executable, args=["-m", "nx_mcp.server"], env=env)
    saved, failures = {}, 0
    with open(os.devnull, "w") as quiet:
        async with (
            stdio_client(params, errlog=quiet) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            for index, step in enumerate(steps, 1):
                tool = step["tool"]
                try:
                    args = resolve(step.get("args", {}), saved)
                except (KeyError, IndexError, ValueError) as error:
                    shapes = {label: sorted(body) for label, body in saved.items()}
                    print(f"[{index}] FAIL {tool}  bad reference {error}; saved keys: {shapes}")
                    failures += 1
                    break
                direct = tool in {"nx_status", "nx_screenshot"}
                if not direct and step.get("mutation", True) and "operation_id" not in args:
                    args = {**args, "operation_id": "dev-" + uuid.uuid4().hex[:12]}
                if direct:
                    result = await session.call_tool(tool, args)
                else:
                    call = {"tool": tool, "arguments": args, "detail": "full"}
                    if step.get("intent"):
                        call["intent"] = step["intent"]
                    result = await session.call_tool("nx_invoke", call)
                body = result.structuredContent
                if body is None:
                    body = [getattr(c, "text", c.type) for c in result.content]
                ok = not result.isError and not (
                    isinstance(body, dict) and body.get("status") == "error"
                )
                expected = step.get("expect_error", False)
                mark = "OK  " if ok else ("ERR*" if expected else "FAIL")
                print(f"[{index}] {mark} {tool}  {short(body, limit)}")
                if step.get("save") and isinstance(body, dict):
                    saved[step["save"]] = body
                if not ok and not expected:
                    failures += 1
                    break
    return failures


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("file", nargs="?", help="JSON file with a list of steps")
    parser.add_argument("--inline", help="JSON list of steps")
    parser.add_argument("--limit", type=int, default=600, help="characters shown per result")
    args = parser.parse_args()
    if bool(args.file) == bool(args.inline):
        parser.error("pass a steps file or --inline")
    steps = json.loads(args.inline if args.inline else Path(args.file).read_text(encoding="utf-8"))
    sys.exit(1 if asyncio.run(run(steps, args.limit)) else 0)


if __name__ == "__main__":
    main()

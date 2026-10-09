"""Create / close the disposable bench parts under smoke/bench_control/.

python scripts/bench/bench_parts.py create small --blocks 1
python scripts/bench/bench_parts.py create heavy --blocks 25 --datums 25
python scripts/bench/bench_parts.py activate small
python scripts/bench/bench_parts.py close-all      # closes only bench_control parts, no save
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SMOKE, client, nx_json, snippet  # noqa: E402


def call(tool, **args):
    return asyncio.run(
        client().call(tool, {**args, "operation_id": "bench-" + uuid.uuid4().hex[:10]})
    )


CLOSE = """
import json
closed = []
for p in list(session.Parts):
    if "bench_control" in p.FullPath.replace("\\\\", "/") and (not ONLY or p.Leaf == ONLY):
        closed.append(p.Leaf)
        p.Close(NXOpen.BasePart.CloseWholeTree.TrueValue, NXOpen.BasePart.CloseModified.CloseModified, None)
print("JSON:" + json.dumps({"closed": closed, "left": [p.Leaf for p in session.Parts]}))
"""

ACTIVATE = """
import json
for p in session.Parts:
    if p.Leaf == NAME and "bench_control" in p.FullPath.replace("\\\\", "/"):
        session.Parts.SetActiveDisplay(p, NXOpen.DisplayPartOption.AllowAdditional,
                                       NXOpen.PartDisplayPartWorkPartOption.UseLast)
        session.Parts.SetWork(p)
print("JSON:" + json.dumps({"work": session.Parts.Work.Leaf if session.Parts.Work else None}))
"""


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["create", "activate", "close-all", "close"])
    parser.add_argument("name", nargs="?")
    parser.add_argument("--blocks", type=int, default=1)
    parser.add_argument("--datums", type=int, default=0)
    args = parser.parse_args()
    if args.action == "create":
        stamp = time.strftime("%H%M%S")
        path = f"{SMOKE}/{args.name}_{stamp}.prt"
        result = call("nx_create_part", path=path, units="mm")
        print(json.dumps(result)[:400])
        t = time.perf_counter()
        print(
            nx_json(snippet("build_part.py"), blocks=args.blocks, datums=args.datums),
            f"{time.perf_counter() - t:.1f}s",
        )
    elif args.action == "activate":
        print(nx_json(f"NAME = {args.name!r}\n" + ACTIVATE))
    else:
        only = args.name if args.action == "close" else None
        print(nx_json(f"ONLY = {only!r}\n" + CLOSE))


if __name__ == "__main__":
    main()

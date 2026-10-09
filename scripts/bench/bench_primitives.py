"""1. Raw handover primitives on the NX UI thread (LockAccess, EnableWindow, ...).

python scripts/bench/bench_primitives.py --n 200
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import nx_json, save, snippet, table  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=200)
    args = parser.parse_args()
    data = nx_json(snippet("primitives.py"), n=args.n)
    print("entry", data["entry"], "exit", data["exit"], "nesting", data["nesting"])
    rows = list(data["ms"].items())
    table(rows, "Raw primitives on the NX UI thread")
    save(
        "primitives",
        {"meta": {k: data[k] for k in ("entry", "exit", "nesting")}, "stats": dict(rows)},
    )


if __name__ == "__main__":
    main()

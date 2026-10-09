"""2. Host transitions control('manual') / control('agent') as implemented.

python scripts/bench/bench_transitions.py --label no_part --n 50
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import nx_json, save, snippet, table  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--label", required=True)
    parser.add_argument("--no-populate", action="store_true")
    args = parser.parse_args()
    data = nx_json(snippet("transitions.py"), n=args.n, populate=not args.no_populate)
    print("state", data["state"])
    rows = list(data["ms"].items())
    table(rows, f"Host transitions ({args.label})")
    save(f"transitions_{args.label}", data)


if __name__ == "__main__":
    main()

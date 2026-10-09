"""4. Change-detection candidates: cost and reliability against simulated engineer edits.

python scripts/bench/bench_detect.py --label heavy           # cost only, current work part
python scripts/bench/bench_detect.py --label small --scenarios
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import nx_json, save, snippet, table  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--label", required=True)
    parser.add_argument("--scenarios", action="store_true")
    args = parser.parse_args()
    data = nx_json(snippet("detect.py"), n=args.n, scenarios=args.scenarios)
    print("work", data["work"], "uv", data["uv"])
    table(list(data["cost"].items()), f"Change-detection cost ({args.label})")
    if data["detect"]:
        names = list(next(iter(data["detect"].values())))
        print("\n== Detected? (rows: engineer action; columns: candidate)")
        for i, n in enumerate(names):
            print(f"  [{i}] {n}")
        print(f"{'':42}" + "".join(f"[{i}]".rjust(5) for i in range(len(names))))
        for scen, row in data["detect"].items():
            print(f"{scen:42}" + "".join(("yes" if row[n] else "-").rjust(5) for n in names))
    save(f"detect_{args.label}", data)


if __name__ == "__main__":
    main()

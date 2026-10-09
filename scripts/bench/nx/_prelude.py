# Prepended to every benchmark snippet: summary statistics computed inside NX
# (nx_dev_python returns at most 20 000 characters of stdout).
def _pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(values):
    xs = sorted(values)
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "p50": round(_pct(xs, 0.5), 4),
        "p95": round(_pct(xs, 0.95), 4),
        "max": round(xs[-1], 4),
        "mean": round(sum(xs) / len(xs), 4),
    }

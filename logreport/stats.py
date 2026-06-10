"""Generic numeric helpers reused across projects."""


def stats(ms_list):
    """count / avg / p50 / p90 / p95 / max for a list of numbers."""
    if not ms_list:
        return {'count': 0, 'avg': 0, 'p50': 0, 'p90': 0, 'p95': 0, 'max': 0}
    s = sorted(ms_list)
    n = len(s)
    def p(pct): return s[min(int(n * pct), n - 1)]
    return {'count': n, 'avg': int(sum(s) / n),
            'p50': p(0.50), 'p90': p(0.90), 'p95': p(0.95), 'max': s[-1]}


def merge_counts(a, b):
    """Sum two {key: count} dicts into a new dict (a + b)."""
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0) + v
    return out

"""Incremental aggregation cache.

Rotated .gz logs are immutable: aggregate them once, cache the result keyed by a
signature of the .gz files, and only reprocess the live (uncompressed) log each
run. Cuts the per-run cost ~5x. The per-project extraction is supplied via a
Profile of callables, so this engine stays domain-agnostic.
"""

import glob
import hashlib
import os
import pickle
from dataclasses import dataclass
from typing import Callable

from .parsing import parse_sorted
from .stats import merge_counts

# Bump when parsing/aggregation logic changes, to invalidate cached .gz aggregates.
CACHE_VERSION = 1


@dataclass
class Profile:
    """Per-project extraction hooks the engine needs to build a report.

    accumulate(events)        -> raw activity aggregates (mergeable)
    scan(events)              -> (ops, logins, http_by_hour)
    merge_activity(a, b)      -> merged raw activity (a precedes b in time)
    finalize_activity(raw)    -> final 'data' dict for rendering
    perf_from_scan(ops, logins, http_by_hour) -> final 'perf_data' dict
    """
    accumulate: Callable
    scan: Callable
    merge_activity: Callable
    finalize_activity: Callable
    perf_from_scan: Callable


def _gz_signature(gz_files):
    parts = []
    for f in sorted(gz_files):
        st = os.stat(f)
        parts.append(f'{os.path.basename(f)}:{st.st_size}:{st.st_mtime_ns}')
    return hashlib.sha256((f'v{CACHE_VERSION}|' + '|'.join(parts)).encode()).hexdigest()


def _gz_partials(profile, gz_files, cache_path, log):
    """(raw_activity, scan_perf) for the immutable .gz logs, via a pickle cache."""
    sig = _gz_signature(gz_files) if gz_files else f'v{CACHE_VERSION}|empty'
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, 'rb') as fh:
                cached = pickle.load(fh)
            if cached.get('sig') == sig:
                log('  caché .gz: HIT (historia reutilizada)')
                return cached['activity'], cached['scan']
        except Exception:
            pass
    log('  caché .gz: MISS (reprocesando históricos)')
    events = parse_sorted(gz_files)
    activity_raw = profile.accumulate(events)
    ops, logins, http = profile.scan(events)
    scan = (ops, logins, dict(http))
    if cache_path:
        try:
            tmp = cache_path + '.tmp'
            with open(tmp, 'wb') as fh:
                pickle.dump({'sig': sig, 'activity': activity_raw, 'scan': scan},
                            fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, cache_path)
        except Exception:
            pass
    return activity_raw, scan


def build_report_data(profile, log_dir, pattern, cache_path=None, log=lambda _m: None):
    """Parse logs into (data, perf_data), reusing cached aggregates for the
    immutable .gz history and only reprocessing the live log each run."""
    files = glob.glob(os.path.join(log_dir, pattern))
    gz_files   = [f for f in files if f.endswith('.gz')]
    live_files = [f for f in files if not f.endswith('.gz')]

    act_gz, scan_gz = _gz_partials(profile, gz_files, cache_path, log)

    live_events = parse_sorted(live_files)
    act_live = profile.accumulate(live_events)
    ops_l, logins_l, http_l = profile.scan(live_events)

    data = profile.finalize_activity(profile.merge_activity(act_gz, act_live))
    perf_data = profile.perf_from_scan(scan_gz[0] + ops_l,
                                       scan_gz[1] + logins_l,
                                       merge_counts(scan_gz[2], http_l))
    return data, perf_data

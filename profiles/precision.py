"""Precision plugin.

Reuses the extraction + render that already live in activity.py (the original
Scarab report), exposed as a plugin for the generic runner. Output is identical
to running activity.py directly.
"""

import activity
import logreport

NAME = 'precision'

PROFILE = logreport.Profile(
    accumulate=activity._accumulate_activity,
    scan=activity._scan_events,
    merge_activity=activity._merge_activity,
    finalize_activity=activity._finalize_activity,
    perf_from_scan=activity._perf_from_scan,
)


def render_activity(data, cfg):
    daily = activity.extract_daily(data)
    return activity.html_report(data, daily, max_reports=cfg.max_reports,
                                top_farms_n=cfg.top_farms, refresh_seconds=cfg.refresh)


def render_perf(perf_data, cfg):
    daily_perf = activity.extract_daily_perf(perf_data)
    return activity.perf_report(perf_data, daily_perf, refresh_seconds=cfg.refresh)

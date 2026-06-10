"""logreport — reusable engine for HTML activity/performance reports from logs.

Generic, domain-agnostic building blocks:
- parsing:  Spring Boot log line format -> event dicts
- cache:    incremental aggregation cache over immutable .gz history + Profile
- stats:    percentiles and count merging
- output:   atomic HTML write + precompressed .gz sibling

Per-project specifics (what to extract, how to categorize errors, what to render)
live in a project profile module (see profiles/scarab.py for the reference one).
"""

from .cache import CACHE_VERSION, Profile, build_report_data
from .output import esc, generated_at_iso, write_report
from .parsing import LOG_PATTERN, parse_file, parse_logs, parse_sorted
from .stats import merge_counts, stats

__all__ = [
    'CACHE_VERSION', 'Profile', 'build_report_data',
    'esc', 'generated_at_iso', 'write_report',
    'LOG_PATTERN', 'parse_file', 'parse_logs', 'parse_sorted',
    'merge_counts', 'stats',
]

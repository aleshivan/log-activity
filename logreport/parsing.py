"""Log parsing — Spring Boot line format (timestamp, level, pid, thread, logger, msg).

Generic across projects that emit this log format. Multi-line entries (stack
traces, etc.) are attached to the preceding event via its 'extra' list.
"""

import glob
import gzip
import os
import re
from datetime import datetime

LOG_PATTERN = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[.]\d+)'  # timestamp
    r'\s+(\w+)\s+\d+\s+---'                            # level + pid
    r'\s+\[([^\]]+)\]'                                  # thread
    r'\s+([\w.$]+)\s+:\s*(.*)$'                          # logger + message (space after : optional)
)


def parse_file(path):
    """Yield parsed event dicts from a single log file (plain or .gz)."""
    current = None
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rt', errors='replace') as f:
        for raw in f:
            line = raw.rstrip('\n')
            m = LOG_PATTERN.match(line.strip())
            if m:
                if current:
                    yield current
                ts_str, level, thread, logger, msg = m.groups()
                try:
                    ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S.%f')
                except ValueError:
                    current = None
                    continue
                current = {
                    'ts': ts, 'level': level, 'thread': thread,
                    'logger': logger, 'msg': msg, 'extra': [],
                }
            elif current and line.strip():
                current['extra'].append(line)
    if current:
        yield current


def parse_sorted(files):
    """Parse the given files and return all events sorted by timestamp."""
    events = []
    for path in sorted(files):
        events.extend(parse_file(path))
    events.sort(key=lambda e: e['ts'])
    return events


def parse_logs(log_dir, pattern):
    """Parse all log files matching pattern inside log_dir, sorted by timestamp."""
    return parse_sorted(glob.glob(os.path.join(log_dir, pattern)))

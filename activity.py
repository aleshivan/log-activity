#!/usr/bin/env python3
"""Parse Scarab Precision log files and generate an HTML activity report."""

import configparser
import glob
import gzip
import hashlib
import html as html_mod
import json
import os
import pickle
import re
from collections import defaultdict
from datetime import datetime, timezone

# Bump when parsing/categorization/aggregation logic changes, to invalidate the
# cached aggregates of the immutable .gz logs.
CACHE_VERSION = 1

# ── Tema visual "Verde Scarab" (handoff de diseño) ──────────────────────────────
FONT_LINK = ('<link href="https://fonts.googleapis.com/css2?family=Archivo:'
             'wght@400;500;600;700;800&display=swap" rel="stylesheet">')

THEME_CSS = '''
  /* ===== Tema Verde Scarab (override) ===== */
  :root{
    --green:#12613f;--green-hover:#0b4e31;--green-data:#117c52;--lime:#8ac465;
    --yellow:#f2b705;--teal:#04a6b7;--navy:#003153;--alert:#b54334;
    --bg:#f9f9f9;--ink:#1d2b24;--ink2:#5d6f66;--cb:#dde7e1;
  }
  body{font-family:'Archivo',system-ui,-apple-system,BlinkMacSystemFont,sans-serif;background:var(--bg);color:var(--ink)}
  header{background:var(--green);border-bottom:3px solid var(--yellow)}
  header h1{color:#fff;font-weight:700;letter-spacing:-.01em}
  header small,header .sub{color:#bcd8c9}
  header a{color:#bcd8c9} header a:hover{color:#fff}
  .tz-select{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25);color:#fff}
  .cards .card{border:1px solid var(--cb);box-shadow:0 1px 3px rgba(6,67,42,.05)}
  .cards .card .num{color:var(--navy);font-variant-numeric:tabular-nums}
  .cards .card .lbl{color:var(--ink2);text-transform:uppercase;letter-spacing:.08em;font-weight:600}
  .cards .card:has(.num.amber){background:#fdf8ea;border-color:#ecc94b}
  .cards .card:has(.num.amber) .num{color:#d69a07}
  .cards .card:has(.num.red){background:#fdf2f0;border-color:#e6988f}
  .cards .card:has(.num.red) .num{color:var(--alert)}
  .cards .card:has(.num.green){background:#eef8f2;border-color:#93c7ab}
  .cards .card:has(.num.green) .num{color:var(--green-data)}
  .cards .card:has(.num:not(.amber):not(.red):not(.green)){background:#eef4fa;border-color:#b6cfe2}
  .day-card{background:#fbfbfb;border:1px solid #e9e9e9;box-shadow:none}
  .day-card .num{color:var(--navy)} .day-card .num.amber{color:#d69a07}
  .day-card .num.red{color:var(--alert)} .day-card .num.green{color:var(--green-data)}
  .section,.chart-card{border:1px solid var(--cb);box-shadow:0 1px 3px rgba(6,67,42,.05)}
  .section h2,.chart-card h2{color:#111;border-bottom:2px solid #e3ebe6}
  th{background:#f0f2f1;color:#80878d;letter-spacing:.06em}
  tbody tr:nth-child(odd){background:#f6f7f7}
  tbody tr:hover{background:#e7f0eb}
  .ms-slow,.ms-med{color:var(--alert)}
  .day-nav,.pg-btn{background:var(--green);color:#fff;border:none}
  .day-nav:hover,.pg-btn:hover:not(:disabled){background:var(--green-hover)}
  .day-nav:disabled,.pg-btn:disabled{background:#c5d2cb}
  .bar{background:var(--lime)} .bar-farm{background:var(--green-data)}
  .err-filter:focus{border-color:var(--lime)}
  a{color:var(--green-data)}
  .genstamp{text-align:center;color:#9aa6a0;font-size:.74rem;padding:8px 0 28px}
'''

_GENERATED_AT = None


def generated_at_iso():
    """Marca de generación del run en ISO 8601 UTC (p.ej. 2026-06-10T16:02:33Z).

    Se calcula una sola vez por proceso, así todos los reportes y el índice de un
    mismo run comparten exactamente el mismo instante (sirve también de logstamp).
    """
    global _GENERATED_AT
    if _GENERATED_AT is None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        _GENERATED_AT = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    return _GENERATED_AT


LOG_PATTERN = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[.]\d+)'  # timestamp
    r'\s+(\w+)\s+\d+\s+---'                            # level + pid
    r'\s+\[([^\]]+)\]'                                  # thread
    r'\s+([\w.$]+)\s+:\s*(.*)$'                          # logger + message (space after : optional)
)

REPORT_START = re.compile(r'\[Report request\]\s+(.*)')
PDF_END      = re.compile(r'\[PDF - (.+?)\]\s+End\s+\[(.+?),\s+Generated\s+(\d+)\s+label\(s\)\s+in\s+(\d+)ms\]')
XLSX_END     = re.compile(r'\[XLSX - (.+?)\]\s+End\s+\((\d+)ms\)')
CSV_END      = re.compile(r'\[CSV - (.+?)\]\s+End\s+\((\d+)ms\)')
FILE_DL      = re.compile(r'\[Files\]\s+File downloaded and removed\s+\.\.\.\s+(.+)')
MAP_END      = re.compile(r'\[PDF - Maps\]\s+End\s+\[(.+?),\s+(\d+)\s+sheet\(s\)\]')

FMT_PDF_MAP = 'PDF/Map'


# ── Log parsing ────────────────────────────────────────────────────────────────

def _parse_file(path):
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


def parse_logs(log_dir, pattern):
    """Parse all log files matching pattern inside log_dir, sorted by timestamp."""
    return _parse_sorted(glob.glob(os.path.join(log_dir, pattern)))


def _parse_sorted(files):
    events = []
    for path in sorted(files):
        events.extend(_parse_file(path))
    events.sort(key=lambda e: e['ts'])
    return events


# ── Incremental aggregation cache ──────────────────────────────────────────────
# Rotated .gz logs are immutable: aggregate them once, cache the result, and only
# reprocess the live (uncompressed) log each run. Cuts the per-run cost ~5x.

def _gz_signature(gz_files):
    parts = []
    for f in sorted(gz_files):
        st = os.stat(f)
        parts.append(f'{os.path.basename(f)}:{st.st_size}:{st.st_mtime_ns}')
    return hashlib.sha256((f'v{CACHE_VERSION}|' + '|'.join(parts)).encode()).hexdigest()


def _gz_partials(gz_files, cache_path, log):
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
    events = _parse_sorted(gz_files)
    activity_raw = _accumulate_activity(events)
    ops, logins, http = _scan_events(events)
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


def build_report_data(log_dir, pattern, cache_path=None, log=lambda _m: None):
    """Parse logs into (data, perf_data), reusing cached aggregates for the
    immutable .gz history and only reprocessing the live log each run."""
    files = glob.glob(os.path.join(log_dir, pattern))
    gz_files   = [f for f in files if f.endswith('.gz')]
    live_files = [f for f in files if not f.endswith('.gz')]

    act_gz, scan_gz = _gz_partials(gz_files, cache_path, log)

    live_events = _parse_sorted(live_files)
    act_live = _accumulate_activity(live_events)
    ops_l, logins_l, http_l = _scan_events(live_events)

    data = _finalize_activity(_merge_activity(act_gz, act_live))
    perf_data = _perf_from_scan(scan_gz[0] + ops_l,
                                scan_gz[1] + logins_l,
                                _merge_counts(scan_gz[2], http_l))
    return data, perf_data


# ── Debug info extraction (used for ERROR-level detail panels) ─────────────────

def _extract_app_frames(full):
    """Return deduplicated com.scarab.precision.* real stack frames, short form."""
    seen, frames = set(), []
    for line in full.splitlines():
        stripped = line.strip()
        if stripped.startswith('#') or 'com.scarab.precision.' not in stripped:
            continue
        s = stripped.lstrip('at ')
        clean = re.sub(r'\s*~\[[^\]]*\]', '', s).strip()  # drop ~[jar:version]
        if clean not in seen:
            seen.add(clean)
            frames.append(clean)
    return frames


def _extract_sql(full):
    """Return the SQL statement from a MyBatis ### SQL: block."""
    sql_lines = []
    in_sql = False
    for line in full.splitlines():
        if line.startswith('### SQL:'):
            in_sql = True
            rest = line[8:].strip()
            if rest:
                sql_lines.append(rest)
        elif in_sql:
            if line.startswith(('### ', '; ')):
                break
            sql_lines.append(line.strip())
    return re.sub(r'\s+', ' ', ' '.join(sql_lines)).strip()


def _extract_exception_chain(full):
    """Return up to 4 short exception-type: message entries from the stack."""
    seen, chain = set(), []
    for m in re.finditer(
            r'^([\w.]+(?:Exception|Error)[\w.]*):\s*(.+)',
            full, re.MULTILINE):
        short = m.group(1).split('.')[-1]
        msg   = m.group(2)[:120].strip()
        entry = f'{short}: {msg}'
        if entry not in seen:
            seen.add(entry)
            chain.append(entry)
        if len(chain) == 4:
            break
    return chain


def _extract_mybatis_method(full):
    """Return the mapper method name from a MyBatis error block."""
    m = re.search(r'### The error may involve ([\w.]+)-Inline', full)
    return m.group(1).split('.')[-1] if m else ''


# ── Error categorisation ───────────────────────────────────────────────────────

def _cat_jwt(msg, full):
    m = re.search(
        r'JWT expired at (.+?)[.] Current time: (.+?), a difference of (\d+) milliseconds',
        full)
    if m:
        expired_at, current_time, diff_ms = m.groups()
        diff_h  = int(diff_ms) // 3600000
        summary = f'Expiró {diff_h}h antes del intento'
        detail  = f'Expiró: {expired_at} | Intento: {current_time} | Diferencia: {diff_h}h'
    else:
        summary, detail = msg[:120], ''
    return {'category': 'JWT_EXPIRED', 'label': 'Token JWT expirado',
            'color': 'warn', 'summary': summary, 'detail': detail}


def _cat_db_duplicate(full, logger):
    mc = re.search(r'unique constraint "(\w+)"', full)
    mk = re.search(r'Key \((.+?)\)=\((.+?)\) already exists', full)
    mm = re.findall(r'precision\.mapper\.(\w+)', full)
    constraint = mc.group(1) if mc else '?'
    key_info   = f'{mk.group(1)} = ({mk.group(2)})' if mk else ''
    mapper     = mm[0] if mm else logger.split('.')[-1]
    return {
        'category': 'DB_DUPLICATE_KEY', 'label': 'Clave duplicada en DB',
        'color': 'error',
        'summary': f'{constraint}: {key_info}',
        'detail': f'Mapper: {mapper} | Constraint: {constraint} | {key_info}',
        'debug': {
            'constraint':  constraint,
            'key_info':    key_info,
            'mapper':      _extract_mybatis_method(full) or mapper,
            'sql':         _extract_sql(full),
            'exc_chain':   _extract_exception_chain(full),
            'app_frames':  _extract_app_frames(full),
        },
    }


def _cat_db_tx_aborted(full, logger):
    mm = re.findall(r'precision\.mapper\.(\w+)', full)
    mapper = mm[0] if mm else logger.split('.')[-1]
    return {
        'category': 'DB_TX_ABORTED', 'label': 'Transacción DB abortada',
        'color': 'error',
        'summary': f'Mapper afectado: {mapper}',
        'detail': 'Cascada de un error anterior (clave duplicada) dejó la transacción en estado inválido.',
        'debug': {
            'mapper':     mapper,
            'exc_chain':  _extract_exception_chain(full),
            'app_frames': _extract_app_frames(full),
            'sql':        _extract_sql(full),
        },
    }


def _cat_user_not_found(full):
    me    = re.search(r"correo '(.+?)'", full)
    email = me.group(1) if me else '?'
    return {'category': 'USER_NOT_FOUND', 'label': 'Usuario no encontrado',
            'color': 'warn',
            'summary': f'Email no registrado: {email}',
            'detail': f'Intento de cambio de contraseña para un correo que no existe en la base de datos: {email}'}


def categorize_error(event):
    msg  = event['msg']
    full = msg + '\n' + '\n'.join(event.get('extra', []))

    if 'ExpiredJwtException' in full or 'Invalid token' in full:
        return {**_cat_jwt(msg, full), 'full': full}
    if 'duplicate key value violates unique constraint' in full:
        return {**_cat_db_duplicate(full, event['logger']), 'full': full}
    if 'current transaction is aborted' in full:
        return {**_cat_db_tx_aborted(full, event['logger']), 'full': full}
    if 'intento de cambiar la clave fallo' in full or 'no existe ningun usuario' in full:
        return {**_cat_user_not_found(full), 'full': full}
    if event['logger'].endswith('FallbackController') or 'Missing or invalid Authorization header' in full:
        return {
            'category': 'UNAUTH_PROBE', 'label': 'Sondeo no autorizado',
            'color': 'warn',
            'summary': 'Petición sin autorización al servidor (tráfico de sondeo/escaneo)',
            'detail': event['logger'],
            'full': full,
        }

    # generic fallback
    return {
        'category': 'OTHER', 'label': 'Otro',
        'color': event['level'].lower(),
        'summary': msg[:120],
        'detail': event['logger'],
        'full': full,
    }


# ── Activity extraction ────────────────────────────────────────────────────────

def _update_report_format(reports, report_types, farms, msg):
    """Match report-completion patterns and annotate the last open report."""
    m = PDF_END.search(msg)
    if m:
        _, farm, labels, ms = m.group(1), m.group(2), m.group(3), m.group(4)
        if reports:
            reports[-1].update({'format': 'PDF', 'detail': f'{farm} — {labels} labels en {ms}ms'})
        report_types['PDF'] += 1
        farms[farm.split(',')[0].strip()] += 1
        return

    m = XLSX_END.search(msg)
    if m:
        if reports:
            reports[-1].update({'format': 'XLSX', 'detail': f'{m.group(1)} en {m.group(2)}ms'})
        report_types['XLSX'] += 1
        return

    m = CSV_END.search(msg)
    if m:
        if reports:
            reports[-1].update({'format': 'CSV', 'detail': f'{m.group(1)} en {m.group(2)}ms'})
        report_types['CSV'] += 1
        return

    m = MAP_END.search(msg)
    if m:
        farm, sheets = m.group(1), m.group(2)
        if reports:
            reports[-1].update({'format': FMT_PDF_MAP, 'detail': f'{farm} — {sheets} hoja(s)'})
        report_types[FMT_PDF_MAP] += 1
        farms[farm.strip()] += 1


def _extract_farm_from_json(msg, farms):
    if '"farmname"' not in msg:
        return
    try:
        farm = json.loads(msg).get('farmname', '')
        if farm:
            farms[farm] += 1
    except Exception:
        pass


def _accumulate_activity(events):
    """Raw, mergeable activity aggregates for a list of events (unsorted output).

    Split out from extract_activity so the immutable .gz history can be
    aggregated once and merged with the live log each run (see _merge_activity).
    """
    reports = []
    downloads = []
    categorized_errors = []
    hourly = defaultdict(int)
    report_types = defaultdict(int)
    farms = defaultdict(int)

    for e in events:
        msg = e['msg']
        ts  = e['ts']
        hourly[ts.strftime('%Y-%m-%d %H:00')] += 1

        m = REPORT_START.search(msg)
        if m:
            reports.append({'ts': ts, 'type': m.group(1).strip(), 'thread': e['thread']})

        _update_report_format(reports, report_types, farms, msg)

        m = FILE_DL.search(msg)
        if m:
            downloads.append({'ts': ts, 'file': m.group(1).strip()})

        if e['level'] in ('WARN', 'ERROR'):
            cat = categorize_error(e)
            categorized_errors.append({
                'ts': ts, 'level': e['level'],
                'logger': e['logger'], 'thread': e['thread'],
                **cat,
            })

        _extract_farm_from_json(msg, farms)

    return {
        'reports': reports,
        'downloads': downloads,
        'errors': categorized_errors,
        'hourly': dict(hourly),
        'report_types': dict(report_types),
        'farms': dict(farms),
        'total_events': len(events),
    }


def _merge_counts(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0) + v
    return out


def _merge_activity(a, b):
    """Merge two raw activity aggregates (a precedes b in time)."""
    return {
        'reports':      a['reports'] + b['reports'],
        'downloads':    a['downloads'] + b['downloads'],
        'errors':       a['errors'] + b['errors'],
        'hourly':       _merge_counts(a['hourly'], b['hourly']),
        'report_types': _merge_counts(a['report_types'], b['report_types']),
        'farms':        _merge_counts(a['farms'], b['farms']),
        'total_events': a['total_events'] + b['total_events'],
    }


def _finalize_activity(raw):
    """Sort the merged raw aggregates into the final extract_activity shape."""
    return {
        'reports':      raw['reports'],
        'downloads':    raw['downloads'],
        'errors':       raw['errors'],
        'hourly':       dict(sorted(raw['hourly'].items())),
        'report_types': dict(raw['report_types']),
        'farms':        dict(sorted(raw['farms'].items(), key=lambda x: -x[1])),
        'total_events': raw['total_events'],
    }


def extract_activity(events):
    return _finalize_activity(_accumulate_activity(events))


def extract_daily(data):
    """Hourly-resolution UTC series for the daily view.

    Emits parallel arrays keyed by absolute UTC hour so the *client* can bucket
    them into local day + local hour-of-day according to the selected timezone
    (the rest of the report converts UTC→local, so the daily view must too).

    Errors are split into 'errors_app' (genuine app errors) and 'errors_probe'
    (UNAUTH_PROBE noise) so day-to-day comparison is not dominated by scan traffic.
    """
    vol   = defaultdict(int)
    rep   = defaultdict(int)
    dl    = defaultdict(int)
    eapp  = defaultdict(int)
    eprobe = defaultdict(int)
    warn  = defaultdict(int)

    for key, cnt in data['hourly'].items():     # keys: 'YYYY-MM-DD HH:00'
        vol[key[:13]] += cnt
    for r in data['reports']:
        rep[r['ts'].strftime('%Y-%m-%d %H')] += 1
    for d in data['downloads']:
        dl[d['ts'].strftime('%Y-%m-%d %H')] += 1
    for e in data['errors']:
        k = e['ts'].strftime('%Y-%m-%d %H')
        if e['level'] == 'WARN':
            warn[k] += 1
        elif e['category'] == 'UNAUTH_PROBE':
            eprobe[k] += 1
        else:
            eapp[k] += 1

    hours = sorted(set(vol) | set(rep) | set(dl) | set(eapp) | set(eprobe) | set(warn))
    return {
        'hours':        [f'{h[:10]}T{h[11:13]}:00:00Z' for h in hours],
        'vol':          [vol[h]    for h in hours],
        'reports':      [rep[h]    for h in hours],
        'downloads':    [dl[h]     for h in hours],
        'errors_app':   [eapp[h]   for h in hours],
        'errors_probe': [eprobe[h] for h in hours],
        'warns':        [warn[h]   for h in hours],
    }


# ── Performance extraction ─────────────────────────────────────────────────────

_PERF_OPS = [
    ('Excel Spray',      re.compile(r'\[XLSX - Excel Published Spray-Programme\] End \((\d+)ms\)')),
    ('XLSX Etiquetas',   re.compile(r'\[XLSX - Product Labels[^\]]*\] End \((\d+)ms\)')),
    ('CSV Etiquetas',    re.compile(r'\[CSV - Product Labels[^\]]*\] End \((\d+)ms\)')),
    ('XLSX Tablas',      re.compile(r'\[XLSX - Current Tables\] End in (\d+)ms')),
    ('XLSX Particiones', re.compile(r'\[XLSX - Partitions\] End \((\d+)ms\)')),
]
_ZEBRA_PERF = re.compile(
    r'\[PDF - Zebra Product Labels[^\]]*\] End \[(.+?), Generated (\d+) label\(s\) in (\d+)ms\]'
)
_LOGIN_PAT = re.compile(r"login success for user '(.+?)'")
_NET_PAT   = re.compile(r'Retrieving file \.\.\. https?://')
_ISO_FMT   = '%Y-%m-%dT%H:%M:%SZ'


def _stats(ms_list):
    if not ms_list:
        return {'count': 0, 'avg': 0, 'p50': 0, 'p90': 0, 'p95': 0, 'max': 0}
    s = sorted(ms_list)
    n = len(s)
    def p(pct): return s[min(int(n * pct), n - 1)]
    return {'count': n, 'avg': int(sum(s) / n),
            'p50': p(0.50), 'p90': p(0.90), 'p95': p(0.95), 'max': s[-1]}


def _match_op(msg, ts):
    """Return a timed-operation dict for msg, or None if no pattern matches."""
    for op_type, pat in _PERF_OPS:
        m = pat.search(msg)
        if m:
            return {'ts': ts, 'type': op_type, 'ms': int(m.group(1)),
                    'farm': None, 'labels': None, 'ms_per_label': None}
    m = _ZEBRA_PERF.search(msg)
    if m:
        farm   = m.group(1).split(',')[0].strip()
        labels = int(m.group(2))
        ms     = int(m.group(3))
        return {'ts': ts, 'type': 'PDF Etiquetas', 'ms': ms, 'farm': farm,
                'labels': labels, 'ms_per_label': round(ms / labels, 1) if labels else None}
    return None


def _scan_events(events):
    """Scan events and return (ops, logins, http_by_hour)."""
    ops, logins, http_by_hour = [], [], defaultdict(int)
    for e in events:
        msg, ts = e['msg'], e['ts']
        op = _match_op(msg, ts)
        if op:
            ops.append(op)
        m = _LOGIN_PAT.search(msg)
        if m:
            logins.append({'ts': ts, 'user': m.group(1)})
        if _NET_PAT.search(msg):
            http_by_hour[ts.strftime('%Y-%m-%d %H:00')] += 1
    return ops, logins, http_by_hour


def _aggregate(ops, logins, http_by_hour):
    """Aggregate scanned data into stats dicts."""
    by_hour, by_day, by_type, farm_ms = (defaultdict(list) for _ in range(4))
    for op in ops:
        by_hour[op['ts'].strftime('%Y-%m-%d %H:00')].append(op['ms'])
        by_day[op['ts'].strftime('%Y-%m-%d')].append(op['ms'])
        by_type[op['type']].append(op['ms'])
        if op['type'] == 'PDF Etiquetas' and op['ms_per_label'] is not None:
            farm_ms[op['farm']].append(op['ms_per_label'])
    login_by_hour = defaultdict(int)
    for lg in logins:
        login_by_hour[lg['ts'].strftime('%Y-%m-%d %H:00')] += 1
    farm_eff = {f: round(sum(v) / len(v), 1) for f, v in farm_ms.items()}
    return {
        'hourly_stats':  {h: _stats(v) for h, v in sorted(by_hour.items())},
        'daily_stats':   {d: _stats(v) for d, v in sorted(by_day.items())},
        'type_stats':    {t: _stats(v) for t, v in sorted(by_type.items(), key=lambda x: -len(x[1]))},
        'farm_eff':      dict(sorted(farm_eff.items(), key=lambda x: -x[1])[:20]),
        'login_by_hour': dict(sorted(login_by_hour.items())),
        'http_by_hour':  dict(sorted(http_by_hour.items())),
    }


def _perf_from_scan(ops, logins, http_by_hour):
    """Build the final performance dict from (possibly merged) scan data."""
    agg = _aggregate(ops, logins, http_by_hour)
    return {
        'ops':       ops,
        'slow_ops':  sorted(ops, key=lambda x: -x['ms'])[:25],
        'logins':    logins,
        'total_ops': len(ops),
        **agg,
    }


def extract_performance(events):
    return _perf_from_scan(*_scan_events(events))


def extract_daily_perf(perf_data):
    """UTC-resolution performance data for the daily comparison.

    Embeds the raw latency samples tagged with their absolute UTC hour (ops are
    few, ~hundreds) plus hourly login counts, so the client can bucket them into
    local day + hour-of-day per the selected timezone and compute correct
    percentiles over any window — consistent with the rest of the report.
    """
    ops = [[op['ts'].strftime('%Y-%m-%dT%H:00:00Z'), op['ms']] for op in perf_data['ops']]
    login = defaultdict(int)
    for lg in perf_data['logins']:
        login[lg['ts'].strftime('%Y-%m-%d %H')] += 1
    hours = sorted(login)
    return {
        'ops':         ops,
        'login_hours': [f'{h[:10]}T{h[11:13]}:00:00Z' for h in hours],
        'logins':      [login[h] for h in hours],
    }


def _ms_class(ms):
    if ms > 500:
        return 'ms-slow'
    if ms > 200:
        return 'ms-med'
    return ''


def _slow_rows(slow_ops):
    rows = ''
    for op in slow_ops:
        iso   = op['ts'].strftime(_ISO_FMT)
        label = op['ts'].strftime('%H:%M:%S')
        farm  = esc(op['farm'] or '—')
        lbl   = op['labels'] if op['labels'] else '—'
        mpl   = str(op['ms_per_label']) if op['ms_per_label'] else '—'
        cls   = _ms_class(op['ms'])
        rows += (
            f'<tr>'
            f'<td class="mono ts-hms" data-utc="{iso}">{label}</td>'
            f'<td>{esc(op["type"])}</td><td>{farm}</td>'
            f'<td class="num-cell {cls}">{op["ms"]}</td>'
            f'<td class="num-cell">{lbl}</td>'
            f'<td class="num-cell">{mpl}</td>'
            f'</tr>'
        )
    return rows


def _daily_rows(daily_stats):
    rows = ''
    for day, st in daily_stats.items():
        rows += (
            f'<tr><td class="mono">{day}</td>'
            f'<td class="num-cell">{st["count"]}</td>'
            f'<td class="num-cell">{st["avg"]}</td>'
            f'<td class="num-cell">{st["p50"]}</td>'
            f'<td class="num-cell">{st["p90"]}</td>'
            f'<td class="num-cell">{st["p95"]}</td>'
            f'<td class="num-cell ms-slow">{st["max"]}</td></tr>'
        )
    return rows


def _login_rows(logins):
    rows = ''
    for lg in sorted(logins, key=lambda x: x['ts'], reverse=True)[:50]:
        iso   = lg['ts'].strftime(_ISO_FMT)
        label = lg['ts'].strftime('%Y-%m-%d %H:%M:%S')
        rows += (
            f'<tr>'
            f'<td class="mono ts-full" data-utc="{iso}">{label}</td>'
            f'<td>{esc(lg["user"])}</td>'
            f'</tr>'
        )
    return rows


# ── Performance HTML report ────────────────────────────────────────────────────

def perf_report(perf_data, daily_perf, refresh_seconds=1200):
    dperf_json = json.dumps(daily_perf)  # UTC series; days bucketed client-side per tz

    hs     = perf_data['hourly_stats']
    hours  = list(hs.keys())
    h_iso  = json.dumps([f'{h[:10]}T{h[11:]}:00Z' for h in hours])
    h_avg  = json.dumps([hs[h]['avg']   for h in hours])
    h_p90  = json.dumps([hs[h]['p90']   for h in hours])
    h_p95  = json.dumps([hs[h]['p95']   for h in hours])
    h_max  = json.dumps([hs[h]['max']   for h in hours])
    h_cnt  = json.dumps([hs[h]['count'] for h in hours])

    ts     = perf_data['type_stats']
    t_names = json.dumps(list(ts.keys()))
    t_avg   = json.dumps([ts[t]['avg']   for t in ts])
    t_p95   = json.dumps([ts[t]['p95']   for t in ts])
    t_cnt   = json.dumps([ts[t]['count'] for t in ts])

    fe      = perf_data['farm_eff']
    f_names = json.dumps(list(fe.keys()))
    f_vals  = json.dumps(list(fe.values()))

    lbh     = perf_data['login_by_hour']
    lh_iso  = json.dumps([f'{h[:10]}T{h[11:]}:00Z' for h in lbh])
    lh_cnt  = json.dumps(list(lbh.values()))

    http_h  = perf_data['http_by_hour']
    net_iso = json.dumps([f'{h[:10]}T{h[11:]}:00Z' for h in http_h])
    net_cnt = json.dumps(list(http_h.values()))

    # Overall stats
    all_ms  = [op['ms'] for op in perf_data['ops']]
    overall = _stats(all_ms)

    slow_rows  = _slow_rows(perf_data['slow_ops'])
    daily_rows = _daily_rows(perf_data['daily_stats'])
    login_rows = _login_rows(perf_data['logins'])

    tz_options = '''
      <option value="UTC">UTC</option>
      <option value="America/Bogota">GMT-5 — Colombia</option>
      <option value="America/Lima">GMT-5 — Perú</option>
      <option value="America/New_York">GMT-5/-4 — New York</option>
      <option value="America/Mexico_City">GMT-6/-5 — México</option>
      <option value="America/Santiago">GMT-4/-3 — Chile</option>
      <option value="Africa/Nairobi">GMT+3 — Nairobi</option>
      <option value="Europe/Madrid">GMT+1/+2 — Madrid</option>'''

    return f'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
{FONT_LINK}
<meta http-equiv="refresh" content="{refresh_seconds}">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Scarab Precision — Rendimiento</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f4f6f9;color:#333}}
  header{{background:#1a2744;color:#fff;padding:20px 32px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}}
  header h1{{font-size:1.4rem;font-weight:600}}
  header small{{opacity:.7;font-size:.85rem}}
  nav a{{color:rgba(255,255,255,.7);font-size:.85rem;text-decoration:none;margin-left:16px}}
  nav a:hover{{color:#fff}}
  .tz-select{{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:6px;padding:4px 10px;font-size:.82rem;cursor:pointer;outline:none}}
  .tz-select option{{background:#1a2744}}
  .container{{max-width:1280px;margin:24px auto;padding:0 16px}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:14px;margin-bottom:24px}}
  .card{{background:#fff;border-radius:8px;padding:18px;box-shadow:0 1px 4px rgba(0,0,0,.08);text-align:center}}
  .card .num{{font-size:1.8rem;font-weight:700;color:#1a2744}}
  .card .num.red{{color:#dc2626}} .card .num.amber{{color:#d97706}} .card .num.green{{color:#16a34a}}
  .card .lbl{{font-size:.72rem;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:.05em}}
  .charts-grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px}}
  @media(max-width:900px){{.charts-grid{{grid-template-columns:1fr}}}}
  .chart-card{{background:#fff;border-radius:8px;padding:20px 24px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  .chart-card h2{{font-size:.95rem;font-weight:600;color:#1a2744;border-bottom:2px solid #e8ecf0;padding-bottom:8px;margin-bottom:16px}}
  .chart-card.wide{{grid-column:1 / -1}}
  .section{{background:#fff;border-radius:8px;padding:20px 24px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:24px}}
  .section h2{{font-size:.95rem;font-weight:600;color:#1a2744;border-bottom:2px solid #e8ecf0;padding-bottom:8px;margin-bottom:16px}}
  table{{width:100%;border-collapse:collapse;font-size:.855rem}}
  th{{text-align:left;padding:6px 10px;background:#f0f3f7;color:#555;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}}
  td{{padding:7px 10px;border-bottom:1px solid #f0f0f0;vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  .mono{{font-family:monospace;font-size:.8rem;white-space:nowrap}}
  .num-cell{{text-align:right;font-family:monospace;font-size:.82rem}}
  .ms-slow{{color:#dc2626;font-weight:600}}
  .ms-med{{color:#d97706;font-weight:600}}
  .scrollable{{max-height:420px;overflow-y:auto}}
  canvas{{max-height:300px}}
  /* ── Vista diaria ── */
  .day-bar{{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:16px}}
  .day-nav{{background:#1a2744;color:#fff;border:none;border-radius:6px;width:32px;height:32px;font-size:1.1rem;cursor:pointer;line-height:1}}
  .day-nav:hover{{background:#2c3e63}} .day-nav:disabled{{opacity:.3;cursor:default}}
  .day-sel{{border:1px solid #cbd5e1;border-radius:6px;padding:5px 10px;font-size:.9rem;cursor:pointer;background:#fff}}
  .day-cmp-lbl{{font-size:.82rem;color:#666;margin-left:8px}}
  .day-badge{{display:inline-block;background:#fef3c7;color:#92400e;border-radius:12px;padding:2px 10px;font-size:.72rem;font-weight:600}}
  .day-note{{font-size:.78rem;color:#888;margin:10px 0 0}}
  .day-cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin-bottom:18px}}
  .day-card{{background:#f8fafc;border:1px solid #e8ecf0;border-radius:8px;padding:14px;text-align:center}}
  .day-card .num{{font-size:1.6rem;font-weight:700;color:#1a2744}}
  .day-card .num.red{{color:#dc2626}} .day-card .num.amber{{color:#d97706}} .day-card .num.green{{color:#16a34a}}
  .day-card .lbl{{font-size:.7rem;color:#666;margin-top:3px;text-transform:uppercase;letter-spacing:.04em}}
  .day-card .delta{{font-size:.76rem;margin-top:5px;font-weight:600;min-height:1em}}
  .delta.up{{color:#dc2626}} .delta.down{{color:#16a34a}} .delta.flat{{color:#9ca3af}}
  .day-charts{{display:grid;grid-template-columns:2fr 1fr;gap:20px}}
  @media(max-width:900px){{.day-charts{{grid-template-columns:1fr}}}}
  .day-chart-wrap{{position:relative;height:300px}}
{THEME_CSS}
</style>
</head>
<body>
<header>
  <div>
    <h1>Scarab Precision — Dashboard de Rendimiento</h1>
    <small>{perf_data["total_ops"]:,} operaciones analizadas &nbsp;|&nbsp;
      <nav style="display:inline">
        <a href="index.html">← Inicio</a> &nbsp;·&nbsp;
        <a href="activity.html">Ver Actividad</a>
      </nav>
    </small>
  </div>
  <div style="display:flex;align-items:center;gap:12px">
    <select class="tz-select" id="tzSelect">{tz_options}</select>
  </div>
</header>
<div class="container">

  <div class="cards">
    <div class="card"><div class="num">{perf_data["total_ops"]}</div><div class="lbl">Operaciones</div></div>
    <div class="card"><div class="num amber">{overall["avg"]}</div><div class="lbl">Promedio (ms)</div></div>
    <div class="card"><div class="num amber">{overall["p90"]}</div><div class="lbl">P90 (ms)</div></div>
    <div class="card"><div class="num red">{overall["p95"]}</div><div class="lbl">P95 (ms)</div></div>
    <div class="card"><div class="num red">{overall["max"]}</div><div class="lbl">Máximo (ms)</div></div>
    <div class="card"><div class="num green">{len(perf_data["logins"])}</div><div class="lbl">Logins</div></div>
  </div>

  <div class="section">
    <h2>Vista diaria — comparar rendimiento entre días</h2>
    <div class="day-bar">
      <button class="day-nav" id="dpPrev" title="Día anterior">‹</button>
      <select class="day-sel" id="dpSel"></select>
      <button class="day-nav" id="dpNext" title="Día siguiente">›</button>
      <span class="day-badge" id="dpPartial" style="display:none">parcial</span>
      <span class="day-cmp-lbl">Comparar con:</span>
      <select class="day-sel" id="dpCmp"></select>
      <span class="day-cmp-lbl">Métrica latencia:</span>
      <select class="day-sel" id="dpMetric">
        <option value="avg">Promedio</option>
        <option value="p90">P90</option>
        <option value="p95" selected>P95</option>
        <option value="max">Máximo</option>
      </select>
    </div>
    <div class="day-cards">
      <div class="day-card"><div class="num" id="dp_count">—</div><div class="lbl">Operaciones</div><div class="delta" id="dpd_count"></div></div>
      <div class="day-card"><div class="num amber" id="dp_avg">—</div><div class="lbl">Avg (ms)</div><div class="delta" id="dpd_avg"></div></div>
      <div class="day-card"><div class="num amber" id="dp_p90">—</div><div class="lbl">P90 (ms)</div><div class="delta" id="dpd_p90"></div></div>
      <div class="day-card"><div class="num red" id="dp_p95">—</div><div class="lbl">P95 (ms)</div><div class="delta" id="dpd_p95"></div></div>
      <div class="day-card"><div class="num red" id="dp_max">—</div><div class="lbl">Máx (ms)</div><div class="delta" id="dpd_max"></div></div>
      <div class="day-card"><div class="num green" id="dp_logins">—</div><div class="lbl">Logins</div><div class="delta" id="dpd_logins"></div></div>
    </div>
    <div class="day-charts">
      <div class="day-chart-wrap"><canvas id="dpLatChart"></canvas></div>
      <div class="day-chart-wrap"><canvas id="dpVolChart"></canvas></div>
    </div>
    <div class="day-note" id="dpNote"></div>
  </div>

  <div class="charts-grid">
    <div class="chart-card wide">
      <h2>Tiempos de Respuesta por Hora — Promedio · P90 · P95 (todo el rango)</h2>
      <canvas id="rtChart"></canvas>
    </div>
  </div>

  <div class="charts-grid">
    <div class="chart-card">
      <h2>Volumen de Operaciones por Hora</h2>
      <canvas id="volChart"></canvas>
    </div>
    <div class="chart-card">
      <h2>Llamadas HTTP Externas por Hora (imágenes)</h2>
      <canvas id="netChart"></canvas>
    </div>
  </div>

  <div class="charts-grid">
    <div class="chart-card">
      <h2>Rendimiento por Tipo de Operación</h2>
      <canvas id="typeChart"></canvas>
    </div>
    <div class="chart-card">
      <h2>ms / Etiqueta por Finca — PDF Zebra</h2>
      <canvas id="effChart"></canvas>
    </div>
  </div>

  <div class="charts-grid">
    <div class="chart-card">
      <h2>Actividad de Usuarios por Hora</h2>
      <canvas id="loginChart"></canvas>
    </div>
  </div>

  <div class="section">
    <h2>Operaciones Más Lentas (top 25)</h2>
    <div class="scrollable">
      <table>
        <thead><tr>
          <th>Hora</th><th>Tipo</th><th>Finca</th>
          <th style="text-align:right">ms</th>
          <th style="text-align:right">Etiquetas</th>
          <th style="text-align:right">ms/etiq</th>
        </tr></thead>
        <tbody>{slow_rows or '<tr><td colspan="6">Sin datos</td></tr>'}</tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <h2>Resumen Diario</h2>
    <table>
      <thead><tr>
        <th>Fecha</th><th style="text-align:right">#</th>
        <th style="text-align:right">Avg</th><th style="text-align:right">P50</th>
        <th style="text-align:right">P90</th><th style="text-align:right">P95</th>
        <th style="text-align:right">Máx</th>
      </tr></thead>
      <tbody>{daily_rows or '<tr><td colspan="7">Sin datos</td></tr>'}</tbody>
    </table>
  </div>

  <div class="section">
    <h2>Últimos 50 Logins</h2>
    <div class="scrollable">
      <table>
        <thead><tr><th>Timestamp</th><th>Usuario</th></tr></thead>
        <tbody>{login_rows or '<tr><td colspan="2">Sin datos</td></tr>'}</tbody>
      </table>
    </div>
  </div>

</div>
<script>
const H_ISO  = {h_iso};
const H_AVG  = {h_avg};
const H_P90  = {h_p90};
const H_P95  = {h_p95};
const H_MAX  = {h_max};
const H_CNT  = {h_cnt};
const T_NAMES = {t_names};
const T_AVG  = {t_avg};
const T_P95  = {t_p95};
const T_CNT  = {t_cnt};
const F_NAMES = {f_names};
const F_VALS = {f_vals};
const LH_ISO = {lh_iso};
const LH_CNT = {lh_cnt};
const NET_ISO = {net_iso};
const NET_CNT = {net_cnt};

function fmtHour(iso, tz) {{
  const d = new Date(iso);
  const p = new Intl.DateTimeFormat('es', {{
    timeZone: tz, month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false
  }}).formatToParts(d);
  const v = {{}};
  p.forEach(x => v[x.type] = x.value);
  return v.day + '/' + v.month + ' ' + v.hour + ':' + v.minute;
}}

function fmtHms(d, tz) {{
  return new Intl.DateTimeFormat('es', {{
    timeZone: tz, hour: '2-digit', minute: '2-digit',
    second: '2-digit', hour12: false
  }}).format(d);
}}

function fmtFull(d, tz) {{
  const p = new Intl.DateTimeFormat('es', {{
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
  }}).formatToParts(d);
  const v = {{}};
  p.forEach(x => v[x.type] = x.value);
  const ms = d.getUTCMilliseconds().toString().padStart(3, '0');
  return v.year + '-' + v.month + '-' + v.day + ' ' + v.hour + ':' + v.minute + ':' + v.second + '.' + ms;
}}

const STORAGE_KEY = 'scarab_tz';
let currentTz = localStorage.getItem(STORAGE_KEY) || 'UTC';

const rtChart = new Chart(document.getElementById('rtChart'), {{
  type: 'line',
  data: {{
    labels: H_ISO.map(i => fmtHour(i, currentTz)),
    datasets: [
      {{ label: 'Promedio', data: H_AVG, borderColor: '#117c52', backgroundColor: 'transparent', tension: 0.3, pointRadius: 3 }},
      {{ label: 'P90',      data: H_P90, borderColor: '#f2b705', backgroundColor: 'transparent', tension: 0.3, pointRadius: 3 }},
      {{ label: 'P95',      data: H_P95, borderColor: '#cc5650', backgroundColor: 'transparent', tension: 0.3, pointRadius: 3 }},
      {{ label: 'Máximo',   data: H_MAX, borderColor: '#8a9a91', backgroundColor: 'transparent', tension: 0.3, pointRadius: 2, borderDash: [4,3] }},
    ]
  }},
  options: {{ responsive: true, plugins: {{ legend: {{ position: 'top' }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'ms' }} }} }} }}
}});

const volChart = new Chart(document.getElementById('volChart'), {{
  type: 'bar',
  data: {{
    labels: H_ISO.map(i => fmtHour(i, currentTz)),
    datasets: [{{ label: 'Operaciones', data: H_CNT, backgroundColor: '#8ac465' }}]
  }},
  options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'Operaciones' }} }} }} }}
}});

const netChart = new Chart(document.getElementById('netChart'), {{
  type: 'bar',
  data: {{
    labels: NET_ISO.map(i => fmtHour(i, currentTz)),
    datasets: [{{ label: 'Llamadas HTTP', data: NET_CNT, backgroundColor: '#04a6b7' }}]
  }},
  options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'Llamadas' }} }} }} }}
}});

const typeChart = new Chart(document.getElementById('typeChart'), {{
  type: 'bar',
  data: {{
    labels: T_NAMES,
    datasets: [
      {{ label: 'Promedio (ms)', data: T_AVG, backgroundColor: '#146b46' }},
      {{ label: 'P95 (ms)',      data: T_P95, backgroundColor: '#e8a09b' }},
    ]
  }},
  options: {{ responsive: true, indexAxis: 'y',
    plugins: {{ legend: {{ position: 'top' }} }},
    scales: {{ x: {{ title: {{ display: true, text: 'ms' }} }} }} }}
}});

const effChart = new Chart(document.getElementById('effChart'), {{
  type: 'bar',
  data: {{
    labels: F_NAMES,
    datasets: [{{ label: 'ms / etiqueta', data: F_VALS, backgroundColor: '#f2b705' }}]
  }},
  options: {{ responsive: true, indexAxis: 'y',
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ title: {{ display: true, text: 'ms/etiq' }} }} }} }}
}});

const loginChart = new Chart(document.getElementById('loginChart'), {{
  type: 'bar',
  data: {{
    labels: LH_ISO.map(i => fmtHour(i, currentTz)),
    datasets: [{{ label: 'Logins', data: LH_CNT, backgroundColor: '#117c52' }}]
  }},
  options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'Logins' }} }} }} }}
}});

const allCharts = [rtChart, volChart, netChart, loginChart];

function applyTz(tz) {{
  allCharts[0].data.labels = H_ISO.map(i => fmtHour(i, tz));
  allCharts[1].data.labels = H_ISO.map(i => fmtHour(i, tz));
  allCharts[2].data.labels = NET_ISO.map(i => fmtHour(i, tz));
  allCharts[3].data.labels = LH_ISO.map(i => fmtHour(i, tz));
  allCharts.forEach(c => c.update());
  document.querySelectorAll('.ts-hms[data-utc]').forEach(el =>
    el.textContent = fmtHms(new Date(el.dataset.utc), tz));
  document.querySelectorAll('.ts-full[data-utc]').forEach(el =>
    el.textContent = fmtFull(new Date(el.dataset.utc), tz));
  localStorage.setItem(STORAGE_KEY, tz);
}}

const sel = document.getElementById('tzSelect');
const saved = localStorage.getItem(STORAGE_KEY);
if (saved) {{
  const opt = sel.querySelector(`option[value="${{saved}}"]`);
  if (opt) {{ opt.selected = true; applyTz(saved); }}
}}
sel.addEventListener('change', () => applyTz(sel.value));

// ── Vista diaria: comparación de rendimiento por día ──
(function () {{
  const DPERF = {dperf_json};              // UTC series: ops [[isoHour,ms]], login_hours[], logins[]
  const DP_KEY = 'scarab_perf_day';
  const STORAGE_KEY = 'scarab_tz';
  const pad = n => String(n).padStart(2, '0');
  const labels = Array.from({{length: 24}}, (_, h) => pad(h) + 'h');

  function stats(arr) {{
    if (!arr.length) return {{ count: 0, avg: 0, p50: 0, p90: 0, p95: 0, max: 0 }};
    const s = [...arr].sort((a, b) => a - b), n = s.length;
    const p = q => s[Math.min(Math.floor(n * q), n - 1)];
    return {{ count: n, avg: Math.round(s.reduce((a, b) => a + b, 0) / n),
      p50: p(.5), p90: p(.9), p95: p(.95), max: s[n - 1] }};
  }}
  // flatten latency samples of a day over an inclusive hour window
  const latWin = (rec, lo, hi) => {{
    let out = [];
    for (let h = lo; h <= hi; h++) out = out.concat(rec.lat[h]);
    return out;
  }};
  const sumWin = (arr, lo, hi) => {{ let t = 0; for (let h = lo; h <= hi; h++) t += arr[h] || 0; return t; }};

  // Bucket an absolute UTC hour into [localDate, localHour] for the chosen tz.
  function localBucket(iso, tz) {{
    const p = new Intl.DateTimeFormat('en-CA', {{
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', hourCycle: 'h23'
    }}).formatToParts(new Date(iso));
    const v = {{}};
    p.forEach(x => v[x.type] = x.value);
    return [`${{v.year}}-${{v.month}}-${{v.day}}`, parseInt(v.hour, 10) % 24];
  }}

  function buildDailyPerf(tz) {{
    const days = {{}};
    const ensure = d => {{
      if (!days[d]) days[d] = {{ lat: Array.from({{length: 24}}, () => []), logins: Array(24).fill(0) }};
      return days[d];
    }};
    DPERF.ops.forEach(([iso, ms]) => {{ const [d, h] = localBucket(iso, tz); ensure(d).lat[h].push(ms); }});
    DPERF.login_hours.forEach((iso, i) => {{ const [d, h] = localBucket(iso, tz); ensure(d).logins[h] += DPERF.logins[i]; }});
    Object.values(days).forEach(rec => {{
      const active = [];
      for (let h = 0; h < 24; h++) if (rec.lat[h].length || rec.logins[h]) active.push(h);
      rec.first_hour = active.length ? active[0] : 0;
      rec.last_hour  = active.length ? active[active.length - 1] : 23;
      rec.partial    = rec.first_hour > 0 || rec.last_hour < 23;
    }});
    return days;
  }}

  let DP = {{}}, DP_DAYS = [], latChart = null, volChart2 = null;

  const dpSel = document.getElementById('dpSel');
  const dpCmp = document.getElementById('dpCmp');
  const dpMetric = document.getElementById('dpMetric');
  const prevBtn = document.getElementById('dpPrev');
  const nextBtn = document.getElementById('dpNext');
  const badge = document.getElementById('dpPartial');
  const note = document.getElementById('dpNote');

  // For latency, an increase is worse: up=red, down=green (matches CSS).
  function setDelta(id, cur, base) {{
    const el = document.getElementById('dpd_' + id);
    if (base == null) {{ el.textContent = ''; el.className = 'delta'; return; }}
    if (base === 0) {{ el.textContent = cur > 0 ? '▲ nuevo' : '–'; el.className = 'delta ' + (cur > 0 ? 'up' : 'flat'); return; }}
    const pct = Math.round((cur - base) / base * 100);
    const dir = pct > 0 ? 'up' : (pct < 0 ? 'down' : 'flat');
    const sign = pct > 0 ? '▲ +' : (pct < 0 ? '▼ ' : '= ');
    el.textContent = `${{sign}}${{pct}}%`;
    el.className = 'delta ' + dir;
  }}

  function fillSelectors() {{
    const cur = dpSel.value, curCmp = dpCmp.value;
    dpSel.innerHTML = '';
    dpCmp.innerHTML = '<option value="">(ninguno)</option>';
    DP_DAYS.forEach(d => {{
      const star = DP[d].partial ? ' *' : '';
      dpSel.insertAdjacentHTML('beforeend', `<option value="${{d}}">${{d}}${{star}}</option>`);
      dpCmp.insertAdjacentHTML('beforeend', `<option value="${{d}}">${{d}}${{star}}</option>`);
    }});
    const savedDay = localStorage.getItem(DP_KEY);
    dpSel.value = (cur && DP[cur]) ? cur
      : (savedDay && DP[savedDay]) ? savedDay
      : DP_DAYS[DP_DAYS.length - 1];
    if (curCmp && DP[curCmp]) dpCmp.value = curCmp;
  }}

  function render() {{
    const day = dpSel.value, cmp = dpCmp.value, metric = dpMetric.value;
    const D = DP[day];
    if (!D) return;
    const C = cmp && DP[cmp] ? DP[cmp] : null;
    badge.style.display = D.partial ? '' : 'none';

    // common hour window
    let lo = D.first_hour, hi = D.last_hour, win = null;
    if (C) {{ lo = Math.max(D.first_hour, C.first_hour); hi = Math.min(D.last_hour, C.last_hour); win = lo <= hi ? [lo, hi] : null; }}

    // cards: full-day values
    const dStat = stats(latWin(D, 0, 23));
    document.getElementById('dp_count').textContent  = dStat.count + (D.partial ? ' *' : '');
    document.getElementById('dp_avg').textContent    = dStat.avg;
    document.getElementById('dp_p90').textContent    = dStat.p90;
    document.getElementById('dp_p95').textContent    = dStat.p95;
    document.getElementById('dp_max').textContent    = dStat.max;
    const dLogins = sumWin(D.logins, 0, 23);
    document.getElementById('dp_logins').textContent = dLogins + (D.partial ? ' *' : '');

    if (C && win) {{
      const cs = stats(latWin(C, win[0], win[1]));
      const ds = stats(latWin(D, win[0], win[1]));
      setDelta('count', ds.count, cs.count);
      setDelta('avg', ds.avg, cs.avg);
      setDelta('p90', ds.p90, cs.p90);
      setDelta('p95', ds.p95, cs.p95);
      setDelta('max', ds.max, cs.max);
      setDelta('logins', sumWin(D.logins, win[0], win[1]), sumWin(C.logins, win[0], win[1]));
    }} else {{
      ['count','avg','p90','p95','max','logins'].forEach(k => setDelta(k, 0, null));
    }}

    // chart data: per hour-of-day metric
    const latSeries = rec => labels.map((_, h) => stats(rec.lat[h])[metric]);
    const volSeries = rec => rec.lat.map(a => a.length);

    const latDs = [{{ label: `${{day}} · ${{metric}}`, data: latSeries(D), borderColor: '#117c52', backgroundColor: 'transparent', tension: .3, pointRadius: 2 }}];
    const volDs = [{{ label: day, data: volSeries(D), backgroundColor: 'rgba(147,197,253,.8)' }}];
    if (C) {{
      latDs.push({{ label: `${{cmp}} · ${{metric}}`, data: latSeries(C), borderColor: '#f2b705', backgroundColor: 'transparent', borderDash: [5,3], tension: .3, pointRadius: 2 }});
      volDs.push({{ label: cmp, data: volSeries(C), backgroundColor: 'rgba(230,81,0,.45)' }});
    }}

    if (latChart) {{ latChart.data.datasets = latDs; latChart.update(); }}
    else latChart = new Chart(document.getElementById('dpLatChart'), {{
      type: 'line', data: {{ labels, datasets: latDs }},
      options: {{ responsive: true, maintainAspectRatio: false,
        plugins: {{ legend: {{ position: 'top' }}, title: {{ display: true, text: 'Latencia por hora del día (ms)' }} }},
        scales: {{ x: {{ title: {{ display: true, text: 'hora del día (local)' }} }}, y: {{ beginAtZero: true, title: {{ display: true, text: 'ms' }} }} }} }}
    }});
    if (volChart2) {{ volChart2.data.datasets = volDs; volChart2.update(); }}
    else volChart2 = new Chart(document.getElementById('dpVolChart'), {{
      type: 'bar', data: {{ labels, datasets: volDs }},
      options: {{ responsive: true, maintainAspectRatio: false,
        plugins: {{ legend: {{ position: 'top' }}, title: {{ display: true, text: 'Volumen de ops por hora del día' }} }},
        scales: {{ x: {{ title: {{ display: true, text: 'hora del día (local)' }} }}, y: {{ beginAtZero: true, title: {{ display: true, text: 'ops' }} }} }} }}
    }});

    let txt = `Día ${{day}}` + (D.partial ? ' (parcial *)' : '') + ` · franja observada ${{pad(D.first_hour)}}–${{pad(D.last_hour)}}h (hora local).`;
    if (C) txt += win ? ` Δ vs ${{cmp}} sobre franja común ${{pad(win[0])}}–${{pad(win[1])}}h (latencia: ▲ rojo = más lento).` : ` Sin franja común con ${{cmp}}.`;
    note.textContent = txt;

    const i = DP_DAYS.indexOf(day);
    prevBtn.disabled = i <= 0; nextBtn.disabled = i >= DP_DAYS.length - 1;
    localStorage.setItem(DP_KEY, day);
  }}

  function step(delta) {{ const i = DP_DAYS.indexOf(dpSel.value) + delta; if (i >= 0 && i < DP_DAYS.length) {{ dpSel.value = DP_DAYS[i]; render(); }} }}

  function rebuild(tz) {{
    DP = buildDailyPerf(tz);
    DP_DAYS = Object.keys(DP).sort();
    fillSelectors();
    render();
  }}

  prevBtn.addEventListener('click', () => step(-1));
  nextBtn.addEventListener('click', () => step(1));
  dpSel.addEventListener('change', render);
  dpCmp.addEventListener('change', render);
  dpMetric.addEventListener('change', render);

  rebuild(localStorage.getItem(STORAGE_KEY) || 'UTC');
  const tzSel = document.getElementById('tzSelect');
  if (tzSel) tzSel.addEventListener('change', () => rebuild(tzSel.value));
}})();
</script>
<script>
if(window.top!==window.self){{var _h=document.querySelector('header');if(_h)_h.style.display='none';}}
function _scarabSetTz(tz){{var s=document.getElementById('tzSelect');if(s&&tz&&s.value!==tz){{s.value=tz;s.dispatchEvent(new Event('change'));}}}}
window.addEventListener('storage',function(e){{if(e.key==='scarab_tz')_scarabSetTz(e.newValue);}});
window.addEventListener('message',function(e){{if(e.data&&e.data.scarabTz)_scarabSetTz(e.data.scarabTz);}});
</script>
<footer class="genstamp">Generado: {generated_at_iso()}</footer>
</body>
</html>'''


# ── HTML rendering ─────────────────────────────────────────────────────────────

CATEGORY_META = {
    'JWT_EXPIRED':     ('JWT expirado',          '#92400e', '#fef3c7', '#f59e0b'),
    'DB_DUPLICATE_KEY':('Clave duplicada en DB', '#991b1b', '#fee2e2', '#ef4444'),
    'DB_TX_ABORTED':   ('TX abortada (cascada)', '#7c3aed', '#ede9fe', '#8b5cf6'),
    'USER_NOT_FOUND':  ('Usuario no encontrado', '#1e40af', '#dbeafe', '#3b82f6'),
    'UNAUTH_PROBE':    ('Sondeo no autorizado',  '#9a3412', '#ffedd5', '#fb923c'),
    'OTHER':           ('Otro',                  '#374151', '#f3f4f6', '#6b7280'),
}

def esc(s):
    return html_mod.escape(str(s))

def _render_err_meta(e):
    iso = e['ts'].strftime('%Y-%m-%dT%H:%M:%S.') + e['ts'].strftime('%f')[:3] + 'Z'
    ts  = e['ts'].strftime('%Y-%m-%d %H:%M:%S.') + e['ts'].strftime('%f')[:3]
    return (
        f'<div class="err-meta">'
        f'<span class="err-ts ts-full" data-utc="{iso}">{esc(ts)}</span>'
        f'<span class="err-badge err-{e["level"].lower()}">{esc(e["level"])}</span>'
        f'<span class="err-logger">{esc(e["logger"])}</span>'
        f'<span class="err-thread">[{esc(e["thread"])}]</span>'
        f'</div>'
    )


def _render_debug_panel(e, uid):
    """Structured debug panel shown for ERROR-level entries."""
    dbg = e.get('debug', {})

    # Exception chain
    chain_html = ''
    for i, exc in enumerate(dbg.get('exc_chain', [])):
        sep = '<span class="exc-arrow">&#8594;</span>' if i else ''
        chain_html += f'{sep}<span class="exc-node">{esc(exc)}</span>'

    # Scarab call frames
    frames_html = ''
    for frame in dbg.get('app_frames', []):
        frames_html += f'<div class="frame-line">{esc(frame)}</div>'

    # SQL block
    sql = dbg.get('sql', '')
    sql_html = (
        f'<div class="dbg-block">'
        f'<div class="dbg-label">SQL ejecutado</div>'
        f'<pre class="sql-pre">{esc(sql)}</pre>'
        f'</div>'
    ) if sql else ''

    chain_block = (
        f'<div class="dbg-block">'
        f'<div class="dbg-label">Cadena de excepciones</div>'
        f'<div class="exc-chain">{chain_html}</div>'
        f'</div>'
    ) if chain_html else ''

    frames_block = (
        f'<div class="dbg-block">'
        f'<div class="dbg-label">Call stack — código Scarab</div>'
        f'<div class="frames">{frames_html}</div>'
        f'</div>'
    ) if frames_html else ''

    stack_id = f'st-{uid}'
    full_text = esc(e.get('full', ''))
    stack_block = (
        f'<details class="stack-details">'
        f'<summary class="stack-summary">'
        f'Stack trace completo'
        f'<button class="copy-btn" data-target="{stack_id}">Copiar</button>'
        f'</summary>'
        f'<pre class="stack" id="{stack_id}">{full_text}</pre>'
        f'</details>'
    ) if full_text else ''

    return (
        f'<div class="debug-panel">'
        f'{chain_block}{frames_block}{sql_html}{stack_block}'
        f'</div>'
    )


def _render_error_item(e, uid, is_error):
    muted = ' muted' if e.get('category') == 'DB_TX_ABORTED' else ''
    meta  = _render_err_meta(e)
    summary = f'<div class="err-summary-line">{esc(e["summary"])}</div>'
    detail  = (f'<div class="err-detail">{esc(e["detail"])}</div>'
               if e.get('detail') else '')

    if is_error:
        body = _render_debug_panel(e, uid)
    else:
        full_text = esc(e.get('full', ''))
        body = (
            f'<details><summary>Ver stack trace</summary>'
            f'<pre class="stack">{full_text}</pre></details>'
        ) if full_text else ''

    return (
        f'<div class="err-item{muted}">'
        f'{meta}{summary}{detail}{body}'
        f'</div>'
    )


def build_error_html(errors):
    if not errors:
        return '<p style="color:#27ae60;padding:12px">Sin errores registrados.</p>'

    by_cat = defaultdict(list)
    for e in errors:
        by_cat[e['category']].append(e)

    # summary pills
    summary_html = '<div class="err-summary">'
    for cat, items in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        label, fg, bg, border = CATEGORY_META.get(cat, CATEGORY_META['OTHER'])
        summary_html += (
            f'<a href="#cat-{cat}" class="err-pill" '
            f'style="background:{bg};color:{fg};border:1px solid {border}">'
            f'{esc(label)} <b>{len(items)}</b></a>'
        )
    summary_html += '</div>'

    cats_html = ''
    for cat, items in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        label, fg, bg, border = CATEGORY_META.get(cat, CATEGORY_META['OTHER'])
        rows = ''.join(
            _render_error_item(e, f'{cat}-{i}', e['level'] == 'ERROR')
            for i, e in enumerate(items)
        )
        cats_html += (
            f'<div class="err-cat" id="cat-{cat}">'
            f'<div class="err-cat-header" '
            f'style="background:{bg};border-left:4px solid {border};color:{fg}">'
            f'<span class="err-cat-label">{esc(label)}</span>'
            f'<span class="err-cat-count">{len(items)} ocurrencia(s)</span>'
            f'</div>'
            f'<div class="err-cat-body">{rows}</div>'
            f'</div>'
        )

    # Lazy stacks: move the heavy <pre class="stack"> bodies (≈74% of the file)
    # out of the initial DOM into a JSON island; inject them on demand (expand/copy).
    stack_store = []

    def _stash(m):
        stack_store.append(m.group(2))
        idattr = m.group(1) or ''
        return f'<pre class="stack lazy"{idattr} data-sid="{len(stack_store) - 1}"></pre>'

    cats_html = re.sub(r'<pre class="stack"( id="[^"]*")?>(.*?)</pre>',
                       _stash, cats_html, flags=re.S)
    stack_island = ('<script type="application/json" id="stackData">'
                    + json.dumps(stack_store) + '</script>')

    copy_js = '''<script>
const STACKS = JSON.parse(document.getElementById('stackData').textContent);
function fillStack(pre) {
  if (!pre || !pre.classList.contains('lazy')) return;
  pre.innerHTML = STACKS[+pre.dataset.sid];
  pre.classList.remove('lazy');
  pre.removeAttribute('data-sid');
}
// Inject a stack only when its <details> is opened.
document.querySelectorAll('details').forEach(d => {
  d.addEventListener('toggle', () => {
    if (d.open) d.querySelectorAll('pre.stack.lazy').forEach(fillStack);
  });
});
document.querySelectorAll('.copy-btn').forEach(btn => {
  btn.addEventListener('click', e => {
    e.stopPropagation();
    const pre = document.getElementById(btn.dataset.target);
    fillStack(pre);  // materialize before copying
    navigator.clipboard.writeText(pre.textContent).then(() => {
      btn.textContent = 'Copiado!';
      setTimeout(() => btn.textContent = 'Copiar', 1800);
    });
  });
});

// ── Paginación + filtro por categoría de errores ──
(function () {
  const PAGE_SIZE = 20;
  document.querySelectorAll('.err-cat').forEach(cat => {
    const body = cat.querySelector('.err-cat-body');
    if (!body) return;
    const items = Array.from(body.querySelectorAll(':scope > .err-item'));
    if (items.length <= PAGE_SIZE) return;  // pocas: sin paginador

    const texts = items.map(el => el.textContent.toLowerCase());
    const bar = document.createElement('div');
    bar.className = 'err-pager';
    const filter = document.createElement('input');
    filter.type = 'search';
    filter.placeholder = 'Filtrar en esta categoría…';
    filter.className = 'err-filter';
    const prev = document.createElement('button'); prev.textContent = '‹ Anterior'; prev.className = 'pg-btn';
    const next = document.createElement('button'); next.textContent = 'Siguiente ›'; next.className = 'pg-btn';
    const info = document.createElement('span'); info.className = 'pg-info';
    bar.append(filter, prev, info, next);
    body.parentNode.insertBefore(bar, body);

    let page = 0, filtered = items;
    function apply() {
      const q = filter.value.trim().toLowerCase();
      filtered = q ? items.filter((_, i) => texts[i].includes(q)) : items;
      const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
      if (page >= pages) page = pages - 1;
      if (page < 0) page = 0;
      items.forEach(el => { el.style.display = 'none'; });
      const start = page * PAGE_SIZE;
      filtered.slice(start, start + PAGE_SIZE).forEach(el => { el.style.display = ''; });
      info.textContent = `Página ${page + 1} / ${pages} · ${filtered.length} ítem(s)`;
      prev.disabled = page <= 0;
      next.disabled = page >= pages - 1;
    }
    prev.addEventListener('click', () => { page--; apply(); });
    next.addEventListener('click', () => { page++; apply(); });
    filter.addEventListener('input', () => { page = 0; apply(); });
    apply();
  });
})();
</script>'''

    return summary_html + cats_html + stack_island + copy_js


def html_report(data, daily, max_reports=60, top_farms_n=60, refresh_seconds=1200):
    hourly = data['hourly']
    hours, counts = list(hourly.keys()), list(hourly.values())
    max_count = max(counts) if counts else 1

    daily_json = json.dumps(daily)  # hourly UTC series; days are bucketed client-side per tz

    bar_rows = ''
    for h, c in zip(hours, counts):
        pct = int(c / max_count * 100)
        iso = f'{h[:10]}T{h[11:]}:00Z'
        bar_rows += (
            f'<tr>'
            f'<td class="hour-label ts-hour" data-utc="{iso}">{h[:10]}<br><b>{h[11:]}</b></td>'
            f'<td><div class="bar" style="width:{pct}%"></div></td>'
            f'<td class="count">{c}</td>'
            f'</tr>'
        )

    report_rows = ''
    for r in data['reports'][-max_reports:]:
        fmt = r.get('format', '—')
        fc  = {'PDF':'badge-pdf','XLSX':'badge-xlsx','CSV':'badge-csv',FMT_PDF_MAP:'badge-map'}.get(fmt,'badge-other')
        iso = r['ts'].strftime(_ISO_FMT)
        report_rows += (
            f'<tr>'
            f'<td class="mono ts-hms" data-utc="{iso}">{r["ts"].strftime("%H:%M:%S")}</td>'
            f'<td><span class="badge {fc}">{fmt}</span></td>'
            f'<td>{esc(r["type"])}</td>'
            f'<td>{esc(r.get("detail",""))}</td>'
            f'</tr>'
        )

    farm_rows = ''
    top_farms = list(data['farms'].items())[:top_farms_n]
    max_farm = top_farms[0][1] if top_farms else 1
    for farm, cnt in top_farms:
        pct = int(cnt / max_farm * 100)
        farm_rows += (
            f'<tr>'
            f'<td>{esc(farm)}</td>'
            f'<td><div class="bar bar-farm" style="width:{pct}%"></div></td>'
            f'<td class="count">{cnt}</td>'
            f'</tr>'
        )

    type_pills = ''.join(
        f'<span class="stat-pill">{t}: <b>{c}</b></span>'
        for t, c in data['report_types'].items()
    )

    last_event     = data['reports'][-1]['ts'].strftime('%Y-%m-%d %H:%M:%S') if data['reports'] else '—'
    last_event_iso = data['reports'][-1]['ts'].strftime(_ISO_FMT) if data['reports'] else ''
    error_html = build_error_html(data['errors'])

    # error count by level for summary cards
    n_err  = sum(1 for e in data['errors'] if e['level'] == 'ERROR')
    n_warn = sum(1 for e in data['errors'] if e['level'] == 'WARN')

    return f'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
{FONT_LINK}
<meta http-equiv="refresh" content="{refresh_seconds}">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Scarab Precision — Actividad</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f4f6f9;color:#333}}
  header{{background:#1a2744;color:#fff;padding:20px 32px}}
  header h1{{font-size:1.4rem;font-weight:600}}
  header small{{opacity:.7;font-size:.85rem}}
  .container{{max-width:1200px;margin:24px auto;padding:0 16px}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px;margin-bottom:24px}}
  .card{{background:#fff;border-radius:8px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.08);text-align:center}}
  .card .num{{font-size:2rem;font-weight:700;color:#1a2744}}
  .card .num.red{{color:#dc2626}}
  .card .num.amber{{color:#d97706}}
  .card .lbl{{font-size:.78rem;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:.05em}}
  .section{{background:#fff;border-radius:8px;padding:20px 24px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:24px}}
  .section h2{{font-size:1rem;font-weight:600;margin-bottom:16px;color:#1a2744;border-bottom:2px solid #e8ecf0;padding-bottom:8px}}
  table{{width:100%;border-collapse:collapse;font-size:.875rem}}
  th{{text-align:left;padding:6px 10px;background:#f0f3f7;color:#555;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}}
  td{{padding:7px 10px;border-bottom:1px solid #f0f0f0;vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  .bar{{background:#4a7fcb;height:18px;border-radius:3px;min-width:2px}}
  .bar-farm{{background:#27ae60}}
  .hour-label{{white-space:nowrap;font-size:.75rem;color:#555;width:90px}}
  .count{{text-align:right;font-size:.85rem;color:#555;width:60px}}
  .mono{{font-family:monospace;font-size:.8rem;white-space:nowrap;width:90px}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600}}
  .badge-pdf{{background:#fde8e8;color:#c0392b}}
  .badge-xlsx{{background:#e8f5e9;color:#27ae60}}
  .badge-csv{{background:#e3f2fd;color:#1565c0}}
  .badge-map{{background:#fff3e0;color:#e65100}}
  .badge-other{{background:#f3e5f5;color:#6a1b9a}}
  .stat-pill{{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:12px;padding:4px 14px;margin:4px;font-size:.85rem}}
  /* ── Error section ── */
  .err-summary{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}}
  .err-pill{{display:inline-block;padding:5px 14px;border-radius:20px;font-size:.82rem;text-decoration:none;transition:opacity .15s}}
  .err-pill:hover{{opacity:.8}}
  .err-cat{{border:1px solid #e5e7eb;border-radius:8px;margin-bottom:14px;overflow:hidden}}
  .err-cat-header{{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;font-size:.9rem}}
  .err-cat-label{{font-weight:600}}
  .err-cat-count{{font-size:.8rem;opacity:.8}}
  .err-cat-body{{padding:0 12px 8px}}
  .err-item{{border-bottom:1px solid #f3f4f6;padding:10px 4px}}
  .err-item:last-child{{border-bottom:none}}
  .err-item.muted{{opacity:.6}}
  .err-meta{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px}}
  .err-ts{{font-family:monospace;font-size:.78rem;color:#6b7280;white-space:nowrap}}
  .err-badge{{font-size:.72rem;font-weight:700;padding:1px 7px;border-radius:3px}}
  .err-error{{background:#fee2e2;color:#991b1b}}
  .err-warn{{background:#fef3c7;color:#92400e}}
  .err-logger{{font-family:monospace;font-size:.75rem;color:#4b5563}}
  .err-thread{{font-family:monospace;font-size:.72rem;color:#9ca3af}}
  .err-summary-line{{font-size:.875rem;color:#111;margin-bottom:2px}}
  .err-detail{{font-size:.8rem;color:#6b7280;margin-bottom:4px}}
  details summary{{font-size:.78rem;color:#2563eb;cursor:pointer;margin-top:4px;user-select:none}}
  details summary:hover{{text-decoration:underline}}
  .stack{{font-family:monospace;font-size:.73rem;background:#1e1e2e;color:#cdd6f4;padding:14px;border-radius:6px;margin-top:8px;overflow-x:auto;white-space:pre;line-height:1.5}}
  /* ── ERROR debug panel ── */
  .debug-panel{{background:#fafafa;border:1px solid #e5e7eb;border-radius:6px;padding:12px 14px;margin-top:8px;display:flex;flex-direction:column;gap:10px}}
  .dbg-block{{display:flex;flex-direction:column;gap:4px}}
  .dbg-label{{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#6b7280}}
  .stack-details{{margin-top:4px}}
  .stack-details>.stack{{margin-top:6px}}
  .stack-summary{{font-size:.78rem;color:#2563eb;cursor:pointer;user-select:none;display:flex;justify-content:space-between;align-items:center;list-style:none;padding:4px 0}}
  .stack-summary::-webkit-details-marker{{display:none}}
  .stack-summary::before{{content:"▶ ";font-size:.7rem;color:#94a3b8;transition:transform .15s}}
  details[open] .stack-summary::before{{content:"▼ "}}
  .exc-chain{{display:flex;flex-wrap:wrap;align-items:center;gap:4px}}
  .exc-node{{font-family:monospace;font-size:.78rem;background:#fff;border:1px solid #e5e7eb;border-radius:4px;padding:2px 8px;color:#1e293b}}
  .exc-arrow{{color:#94a3b8;font-size:.85rem}}
  .frames{{display:flex;flex-direction:column;gap:2px}}
  .frame-line{{font-family:monospace;font-size:.78rem;color:#0f172a;background:#eff6ff;border-left:3px solid #3b82f6;padding:2px 8px;border-radius:0 3px 3px 0}}
  .sql-pre{{font-family:monospace;font-size:.75rem;background:#f0fdf4;color:#14532d;border:1px solid #bbf7d0;padding:10px;border-radius:4px;white-space:pre-wrap;word-break:break-all}}
  .copy-btn{{font-size:.72rem;background:#e0e7ff;color:#3730a3;border:none;border-radius:4px;padding:2px 10px;cursor:pointer;font-weight:600}}
  .copy-btn:hover{{background:#c7d2fe}}
  /* ── Paginador de errores ── */
  .err-pager{{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 4px;border-bottom:1px solid #f0f0f0;margin-bottom:4px}}
  .err-filter{{flex:1;min-width:160px;border:1px solid #cbd5e1;border-radius:6px;padding:5px 10px;font-size:.82rem;outline:none}}
  .err-filter:focus{{border-color:#3b82f6}}
  .pg-btn{{background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe;border-radius:6px;padding:4px 10px;font-size:.8rem;cursor:pointer;font-weight:600}}
  .pg-btn:hover:not(:disabled){{background:#c7d2fe}}
  .pg-btn:disabled{{opacity:.4;cursor:default}}
  .pg-info{{font-size:.8rem;color:#666;white-space:nowrap}}
  .scrollable{{max-height:480px;overflow-y:auto}}
  /* ── Timezone selector ── */
  .tz-select{{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:6px;padding:4px 10px;font-size:.82rem;cursor:pointer;outline:none}}
  .tz-select:hover{{background:rgba(255,255,255,.25)}}
  .tz-select option{{background:#1a2744;color:#fff}}
  /* ── Vista diaria ── */
  .day-bar{{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:18px}}
  .day-nav{{background:#1a2744;color:#fff;border:none;border-radius:6px;width:32px;height:32px;font-size:1.1rem;cursor:pointer;line-height:1}}
  .day-nav:hover{{background:#2c3e63}}
  .day-nav:disabled{{opacity:.3;cursor:default}}
  .day-sel{{border:1px solid #cbd5e1;border-radius:6px;padding:5px 10px;font-size:.9rem;cursor:pointer;background:#fff}}
  .day-cmp-lbl{{font-size:.82rem;color:#666;margin-left:8px}}
  .day-badge{{display:inline-block;background:#fef3c7;color:#92400e;border-radius:12px;padding:2px 10px;font-size:.72rem;font-weight:600}}
  .day-note{{font-size:.78rem;color:#888;margin:6px 0 16px}}
  .day-cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:20px}}
  .day-card{{background:#f8fafc;border:1px solid #e8ecf0;border-radius:8px;padding:14px;text-align:center}}
  .day-card .num{{font-size:1.7rem;font-weight:700;color:#1a2744}}
  .day-card .num.red{{color:#dc2626}} .day-card .num.amber{{color:#d97706}}
  .day-card .lbl{{font-size:.72rem;color:#666;margin-top:3px;text-transform:uppercase;letter-spacing:.04em}}
  .day-card .delta{{font-size:.78rem;margin-top:5px;font-weight:600;min-height:1em}}
  .delta.up{{color:#dc2626}} .delta.down{{color:#16a34a}} .delta.flat{{color:#9ca3af}}
  .day-chart-wrap{{position:relative;height:300px}}
{THEME_CSS}
</style>
</head>
<body>
<header style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
  <div>
    <h1>Scarab Precision — Log de Actividad</h1>
    <small>precision-8443.log &nbsp;|&nbsp;
      Último reporte: <span class="ts-hms" data-utc="{last_event_iso}">{last_event}</span>
      &nbsp;|&nbsp; {data["total_events"]:,} líneas procesadas
      &nbsp;|&nbsp; <nav style="display:inline">
        <a href="index.html" style="color:rgba(255,255,255,.7)">← Inicio</a> ·
        <a href="performance.html" style="color:rgba(255,255,255,.7)">Ver Rendimiento</a>
      </nav>
    </small>
  </div>
  <div>
    <select class="tz-select" id="tzSelect">
      <option value="UTC">UTC</option>
      <option value="America/Bogota">GMT-5 — Colombia</option>
      <option value="America/Lima">GMT-5 — Perú</option>
      <option value="America/New_York">GMT-5/-4 — New York</option>
      <option value="America/Mexico_City">GMT-6/-5 — México</option>
      <option value="America/Santiago">GMT-4/-3 — Chile</option>
      <option value="Africa/Nairobi">GMT+3 — Nairobi</option>
      <option value="Europe/Madrid">GMT+1/+2 — Madrid</option>
    </select>
  </div>
</header>
<div class="container">

  <div class="cards">
    <div class="card"><div class="num">{len(data["reports"])}</div><div class="lbl">Reportes</div></div>
    <div class="card"><div class="num">{len(data["downloads"])}</div><div class="lbl">Descargas</div></div>
    <div class="card"><div class="num">{len(data["farms"])}</div><div class="lbl">Fincas</div></div>
    <div class="card"><div class="num red">{n_err}</div><div class="lbl">Errors</div></div>
    <div class="card"><div class="num amber">{n_warn}</div><div class="lbl">Warnings</div></div>
    <div class="card"><div class="num">{len(hourly)}</div><div class="lbl">Horas activas</div></div>
  </div>

  <div class="section">
    <h2>Vista diaria — comparar días</h2>
    <div class="day-bar">
      <button class="day-nav" id="dayPrev" title="Día anterior">‹</button>
      <select class="day-sel" id="daySel"></select>
      <button class="day-nav" id="dayNext" title="Día siguiente">›</button>
      <span class="day-badge" id="dayPartial" style="display:none">parcial</span>
      <span class="day-cmp-lbl">Comparar con:</span>
      <select class="day-sel" id="cmpSel"></select>
    </div>
    <div class="day-cards">
      <div class="day-card"><div class="num" id="m_reports">—</div><div class="lbl">Reportes</div><div class="delta" id="d_reports"></div></div>
      <div class="day-card"><div class="num" id="m_downloads">—</div><div class="lbl">Descargas</div><div class="delta" id="d_downloads"></div></div>
      <div class="day-card"><div class="num red" id="m_errors_app">—</div><div class="lbl">Errores app</div><div class="delta" id="d_errors_app"></div></div>
      <div class="day-card"><div class="num amber" id="m_errors_probe">—</div><div class="lbl">Sondeo</div><div class="delta" id="d_errors_probe"></div></div>
      <div class="day-card"><div class="num amber" id="m_warns">—</div><div class="lbl">Warnings</div><div class="delta" id="d_warns"></div></div>
    </div>
    <div class="day-chart-wrap"><canvas id="dayChart"></canvas></div>
    <div class="day-note" id="dayNote"></div>
  </div>

  <div class="section">
    <h2>Actividad por hora — acumulada todo el rango</h2>
    <details>
      <summary style="font-size:.85rem;color:#2563eb;cursor:pointer;margin-bottom:12px">Mostrar tabla acumulada ({len(hourly)} horas)</summary>
      <div class="scrollable">
        <table>
          <thead><tr><th>Hora</th><th>Volumen</th><th>#</th></tr></thead>
          <tbody>{bar_rows}</tbody>
        </table>
      </div>
    </details>
  </div>

  <div class="section">
    <h2>Reportes recientes (últimos {max_reports})</h2>
    <div style="margin-bottom:12px">{type_pills}</div>
    <div class="scrollable">
      <table>
        <thead><tr><th>Hora</th><th>Formato</th><th>Tipo</th><th>Detalle</th></tr></thead>
        <tbody>{report_rows or '<tr><td colspan="4">Sin datos</td></tr>'}</tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <h2>Fincas más activas (top {top_farms_n})</h2>
    <div class="scrollable">
      <table>
        <thead><tr><th>Finca</th><th>Volumen</th><th>#</th></tr></thead>
        <tbody>{farm_rows or '<tr><td colspan="3">Sin datos</td></tr>'}</tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <h2>Warnings y Errores — detalle</h2>
    {error_html}
  </div>

</div>
<script>
(function () {{
  const STORAGE_KEY = 'scarab_tz';

  function fmtHms(d, tz) {{
    return new Intl.DateTimeFormat('es', {{
      timeZone: tz, hour: '2-digit', minute: '2-digit',
      second: '2-digit', hour12: false
    }}).format(d);
  }}

  function fmtFull(d, tz) {{
    const p = new Intl.DateTimeFormat('es', {{
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
    }}).formatToParts(d);
    const v = {{}};
    p.forEach(x => v[x.type] = x.value);
    const ms = d.getUTCMilliseconds().toString().padStart(3, '0');
    return `${{v.year}}-${{v.month}}-${{v.day}} ${{v.hour}}:${{v.minute}}:${{v.second}}.${{ms}}`;
  }}

  function fmtHour(d, tz) {{
    const p = new Intl.DateTimeFormat('es', {{
      timeZone: tz, year: 'numeric', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
    }}).formatToParts(d);
    const v = {{}};
    p.forEach(x => v[x.type] = x.value);
    return `${{v.year}}-${{v.month}}-${{v.day}}<br><b>${{v.hour}}:${{v.minute}}</b>`;
  }}

  function applyTz(tz) {{
    document.querySelectorAll('.ts-hms[data-utc]').forEach(el => {{
      el.textContent = fmtHms(new Date(el.dataset.utc), tz);
    }});
    document.querySelectorAll('.ts-full[data-utc]').forEach(el => {{
      el.textContent = fmtFull(new Date(el.dataset.utc), tz);
    }});
    document.querySelectorAll('.ts-hour[data-utc]').forEach(el => {{
      el.innerHTML = fmtHour(new Date(el.dataset.utc), tz);
    }});
    localStorage.setItem(STORAGE_KEY, tz);
  }}

  const sel = document.getElementById('tzSelect');
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved) {{
    const opt = sel.querySelector(`option[value="${{saved}}"]`);
    if (opt) {{ opt.selected = true; applyTz(saved); }}
  }}
  sel.addEventListener('change', () => applyTz(sel.value));
}})();
</script>
<script>
(function () {{
  const SERIES = {daily_json};           // hourly UTC series
  const STORAGE_KEY = 'scarab_tz';
  const DAY_KEY = 'scarab_day';
  const KEYS = ['vol', 'reports', 'downloads', 'errors_app', 'errors_probe', 'warns'];
  const METRICS = ['reports', 'downloads', 'errors_app', 'errors_probe', 'warns'];

  const daySel  = document.getElementById('daySel');
  const cmpSel  = document.getElementById('cmpSel');
  const prevBtn = document.getElementById('dayPrev');
  const nextBtn = document.getElementById('dayNext');
  const badge   = document.getElementById('dayPartial');
  const note    = document.getElementById('dayNote');
  const pad = n => String(n).padStart(2, '0');
  const labels = Array.from({{length: 24}}, (_, h) => pad(h) + 'h');

  let DAILY = {{}}, DAYS = [], chart = null;

  // Bucket an absolute UTC hour into [localDate, localHour] for the chosen tz,
  // so the daily view stays consistent with the rest of the report.
  function localBucket(iso, tz) {{
    const p = new Intl.DateTimeFormat('en-CA', {{
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', hourCycle: 'h23'
    }}).formatToParts(new Date(iso));
    const v = {{}};
    p.forEach(x => v[x.type] = x.value);
    return [`${{v.year}}-${{v.month}}-${{v.day}}`, parseInt(v.hour, 10) % 24];
  }}

  function buildDaily(tz) {{
    const days = {{}};
    SERIES.hours.forEach((iso, i) => {{
      const [d, h] = localBucket(iso, tz);
      if (!days[d]) {{
        days[d] = {{}};
        KEYS.forEach(k => days[d][k] = Array(24).fill(0));
      }}
      KEYS.forEach(k => {{ days[d][k][h] += SERIES[k][i]; }});
    }});
    Object.values(days).forEach(rec => {{
      const active = [];
      for (let h = 0; h < 24; h++) if (rec.vol[h] > 0) active.push(h);
      rec.first_hour = active.length ? active[0] : 0;
      rec.last_hour  = active.length ? active[active.length - 1] : 23;
      rec.partial    = rec.first_hour > 0 || rec.last_hour < 23;
    }});
    return days;
  }}

  function fillSelectors() {{
    const cur = daySel.value, curCmp = cmpSel.value;
    daySel.innerHTML = '';
    cmpSel.innerHTML = '<option value="">(ninguno)</option>';
    DAYS.forEach(d => {{
      const star = DAILY[d].partial ? ' *' : '';
      daySel.insertAdjacentHTML('beforeend', `<option value="${{d}}">${{d}}${{star}}</option>`);
      cmpSel.insertAdjacentHTML('beforeend', `<option value="${{d}}">${{d}}${{star}}</option>`);
    }});
    const savedDay = localStorage.getItem(DAY_KEY);
    daySel.value = (cur && DAILY[cur]) ? cur
      : (savedDay && DAILY[savedDay]) ? savedDay
      : DAYS[DAYS.length - 1];
    if (curCmp && DAILY[curCmp]) cmpSel.value = curCmp;
  }}

  const sum = (arr, lo, hi) => {{ let t = 0; for (let h = lo; h <= hi; h++) t += arr[h] || 0; return t; }};

  function setDelta(id, cur, base) {{
    const el = document.getElementById('d_' + id);
    if (base == null) {{ el.textContent = ''; el.className = 'delta'; return; }}
    if (base === 0) {{
      el.textContent = cur > 0 ? '▲ nuevo' : '–';
      el.className = 'delta ' + (cur > 0 ? 'up' : 'flat');
      return;
    }}
    const pct = Math.round((cur - base) / base * 100);
    const dir = pct > 0 ? 'up' : (pct < 0 ? 'down' : 'flat');
    const sign = pct > 0 ? '▲ +' : (pct < 0 ? '▼ ' : '= ');
    el.textContent = `${{sign}}${{pct}}%`;
    el.className = 'delta ' + dir;
  }}

  function render() {{
    const day = daySel.value;
    const cmp = cmpSel.value;
    const D = DAILY[day];
    if (!D) return;
    const C = cmp && DAILY[cmp] ? DAILY[cmp] : null;

    badge.style.display = D.partial ? '' : 'none';

    let lo = D.first_hour, hi = D.last_hour, win = null;
    if (C) {{
      lo = Math.max(D.first_hour, C.first_hour);
      hi = Math.min(D.last_hour, C.last_hour);
      win = lo <= hi ? [lo, hi] : null;
    }}

    METRICS.forEach(key => {{
      const total = sum(D[key], 0, 23);
      document.getElementById('m_' + key).textContent =
        total.toLocaleString('es') + (D.partial ? ' *' : '');
      if (C && win) setDelta(key, sum(D[key], win[0], win[1]), sum(C[key], win[0], win[1]));
      else setDelta(key, 0, null);
    }});

    let txt = `Totales del día ${{day}}` + (D.partial ? ' (parcial *)' : '') +
              ` · franja observada ${{pad(D.first_hour)}}–${{pad(D.last_hour)}}h (hora local).`;
    if (C) {{
      txt += win
        ? ` Δ comparado con ${{cmp}} sobre la franja común ${{pad(win[0])}}–${{pad(win[1])}}h.`
        : ` Sin franja horaria común con ${{cmp}}: no se puede comparar de forma justa.`;
    }}
    note.textContent = txt;

    drawChart(day, D, cmp, C);
    const i = DAYS.indexOf(day);
    prevBtn.disabled = i <= 0;
    nextBtn.disabled = i >= DAYS.length - 1;
    localStorage.setItem(DAY_KEY, day);
  }}

  function drawChart(day, D, cmp, C) {{
    const ds = [{{
      label: day, data: D.vol,
      backgroundColor: 'rgba(138,196,101,.7)', borderColor: '#8ac465', borderWidth: 1,
    }}];
    if (C) ds.push({{
      label: cmp, data: C.vol, type: 'line',
      borderColor: '#f2b705', backgroundColor: 'rgba(242,183,5,.14)',
      borderWidth: 2, pointRadius: 2, tension: .3, fill: false,
    }});
    if (chart) {{ chart.data.datasets = ds; chart.update(); return; }}
    chart = new Chart(document.getElementById('dayChart'), {{
      type: 'bar',
      data: {{ labels, datasets: ds }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        scales: {{
          x: {{ title: {{ display: true, text: 'hora del día (local)' }} }},
          y: {{ beginAtZero: true, title: {{ display: true, text: 'eventos' }} }},
        }},
        plugins: {{ legend: {{ position: 'top' }} }},
      }},
    }});
  }}

  function step(delta) {{
    const i = DAYS.indexOf(daySel.value) + delta;
    if (i >= 0 && i < DAYS.length) {{ daySel.value = DAYS[i]; render(); }}
  }}

  function rebuild(tz) {{
    DAILY = buildDaily(tz);
    DAYS  = Object.keys(DAILY).sort();
    fillSelectors();
    render();
  }}

  prevBtn.addEventListener('click', () => step(-1));
  nextBtn.addEventListener('click', () => step(1));
  daySel.addEventListener('change', render);
  cmpSel.addEventListener('change', render);

  rebuild(localStorage.getItem(STORAGE_KEY) || 'UTC');
  const tzSel = document.getElementById('tzSelect');
  if (tzSel) tzSel.addEventListener('change', () => rebuild(tzSel.value));
}})();
</script>
<script>
if(window.top!==window.self){{var _h=document.querySelector('header');if(_h)_h.style.display='none';}}
function _scarabSetTz(tz){{var s=document.getElementById('tzSelect');if(s&&tz&&s.value!==tz){{s.value=tz;s.dispatchEvent(new Event('change'));}}}}
window.addEventListener('storage',function(e){{if(e.key==='scarab_tz')_scarabSetTz(e.newValue);}});
window.addEventListener('message',function(e){{if(e.data&&e.data.scarabTz)_scarabSetTz(e.data.scarabTz);}});
</script>
<footer class="genstamp">Generado: {generated_at_iso()}</footer>
</body>
</html>'''


# ── Entry point ────────────────────────────────────────────────────────────────

def _write_report(path, content):
    """Write the HTML and a precompressed .gz sibling (atomically, each).

    nginx `gzip_static on` serves the .gz directly (Content-Encoding: gzip),
    so the ~12 MB report transfers as ~0.2 MB without per-request CPU.
    """
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(content)
    os.replace(tmp, path)

    gz_tmp = path + '.gz.tmp'
    with gzip.open(gz_tmp, 'wt', encoding='utf-8') as f:
        f.write(content)
    os.replace(gz_tmp, path + '.gz')


def index_html(reports, refresh_seconds=1200):
    """Shell con tabs: header verde + barra de tabs + iframe que carga el reporte
    activo. reports = [(label, href), ...]. Los reportes ocultan su propio header
    cuando van embebidos (detectan el iframe), así no hay doble cabecera."""
    tabs = ''.join(
        f'<button class="tab" data-src="{esc(href)}">{esc(label)}</button>'
        for label, href in reports
    )
    frames = ''.join(
        f'<iframe class="frame" data-frame="{esc(href)}" title="{esc(label)}"></iframe>'
        for label, href in reports
    )
    first = esc(reports[0][1]) if reports else ''
    return f'''<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
{FONT_LINK}
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache"><meta http-equiv="Expires" content="0">
<title>Scarab Precision — Reportes</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  html,body{{height:100%}}
  body{{font-family:'Archivo',system-ui,-apple-system,sans-serif;display:flex;flex-direction:column;background:#f9f9f9}}
  header{{background:#12613f;color:#fff;padding:14px 28px;flex:none}}
  header h1{{font-size:1.25rem;font-weight:700;letter-spacing:-.01em}}
  .tabs{{display:flex;gap:2px;background:#0e4d32;padding:0 16px;flex:none;border-bottom:3px solid #f2b705}}
  .tab{{background:none;border:none;color:#bcd8c9;font-family:inherit;font-size:.95rem;font-weight:600;padding:13px 24px;cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-3px}}
  .tab:hover{{color:#fff}}
  .tab.active{{color:#fff;border-bottom-color:#f2b705;background:rgba(255,255,255,.07)}}
  .tzsh{{background:rgba(255,255,255,.12);color:#fff;border:1px solid rgba(255,255,255,.25);border-radius:8px;padding:6px 10px;font-family:inherit;font-size:.85rem;cursor:pointer;outline:none}}
  .tzsh option{{color:#111}}
  .frames{{flex:1;position:relative}}
  .frame{{position:absolute;inset:0;width:100%;height:100%;border:none;background:#f9f9f9}}
</style></head><body>
<header style="display:flex;justify-content:space-between;align-items:center;gap:16px">
  <div>
    <h1>Scarab Precision — Reportes</h1>
    <small style="display:block;color:#bcd8c9;font-size:.78rem;font-weight:400;margin-top:2px">Generado: {generated_at_iso()}</small>
  </div>
  <select id="tzShell" class="tzsh">
    <option value="UTC">UTC</option>
    <option value="America/Bogota">GMT-5 — Colombia</option>
    <option value="America/Lima">GMT-5 — Perú</option>
    <option value="America/New_York">GMT-5/-4 — New York</option>
    <option value="America/Mexico_City">GMT-6/-5 — México</option>
    <option value="America/Santiago">GMT-4/-3 — Chile</option>
    <option value="Africa/Nairobi">GMT+3 — Nairobi</option>
    <option value="Europe/Madrid">GMT+1/+2 — Madrid</option>
  </select>
</header>
<div class="tabs">{tabs}</div>
<div class="frames">{frames}</div>
<script>
(function(){{
  var KEY='scarab_tab';
  var tabs=[].slice.call(document.querySelectorAll('.tab'));
  var frames=[].slice.call(document.querySelectorAll('.frame'));
  function activate(src){{
    tabs.forEach(function(t){{ t.classList.toggle('active', t.dataset.src===src); }});
    frames.forEach(function(f){{
      var on = f.dataset.frame===src;
      if(on && !f.src) f.src = f.dataset.frame;   // carga perezosa, una sola vez
      f.style.display = on ? 'block' : 'none';
    }});
    localStorage.setItem(KEY, src);
  }}
  tabs.forEach(function(t){{ t.addEventListener('click', function(){{ activate(t.dataset.src); }}); }});
  var saved=localStorage.getItem(KEY);
  var valid=tabs.some(function(t){{ return t.dataset.src===saved; }});
  activate(valid ? saved : '{first}');

  // Selector de zona horaria del shell -> se propaga a los reportes (iframes) vía
  // localStorage; cada reporte escucha el evento 'storage' y re-renderiza al instante.
  var TZ='scarab_tz', tzSel=document.getElementById('tzShell');
  var savedTz=localStorage.getItem(TZ)||'UTC';
  var o=tzSel.querySelector('option[value="'+savedTz+'"]'); if(o)o.selected=true;
  tzSel.addEventListener('change', function(){{
    localStorage.setItem(TZ, tzSel.value);  // para frames que se carguen luego (+ evento storage en http)
    frames.forEach(function(f){{ try{{ if(f.contentWindow) f.contentWindow.postMessage({{scarabTz: tzSel.value}}, '*'); }}catch(_e){{}} }});
  }});
}})();
</script>
<script>
if(window.top!==window.self){{var _h=document.querySelector('header');if(_h)_h.style.display='none';}}
function _scarabSetTz(tz){{var s=document.getElementById('tzSelect');if(s&&tz&&s.value!==tz){{s.value=tz;s.dispatchEvent(new Event('change'));}}}}
window.addEventListener('storage',function(e){{if(e.key==='scarab_tz')_scarabSetTz(e.newValue);}});
window.addEventListener('message',function(e){{if(e.data&&e.data.scarabTz)_scarabSetTz(e.data.scarabTz);}});
</script>
</body></html>'''


def _load_config():
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'activity.config')
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(f'Config file not found: {cfg_path}')
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    return cfg


if __name__ == '__main__':
    cfg = _load_config()

    log_dir     = cfg.get('logs',    'dir')
    pattern     = cfg.get('logs',    'pattern')
    output_dir  = cfg.get('output',  'dir')
    output_name = cfg.get('output',  'name',        fallback='activity.html')
    max_reports = cfg.getint('display', 'max_reports', fallback=60)
    top_farms_n = cfg.getint('display', 'top_farms',   fallback=60)
    refresh_s   = cfg.getint('display', 'refresh_seconds', fallback=1200)
    quiet       = cfg.getboolean('display', 'quiet',   fallback=False)

    def log(msg):
        if not quiet:
            print(msg)

    # Logstamp del run en ISO 8601 UTC (mismo instante que el sello de los
    # reportes; siempre se imprime para servir de marca al redirigir a un log).
    print(f'━━━ run @ {generated_at_iso()} ━━━')
    log(f'Log dir : {log_dir}')
    log(f'Pattern : {pattern}')

    cache_path = os.path.join(output_dir, '.report_cache.pkl')
    data, perf_data = build_report_data(log_dir, pattern, cache_path, log)
    log(f'  {data["total_events"]} log entries loaded')

    n_err  = sum(1 for e in data['errors'] if e['level'] == 'ERROR')
    n_warn = sum(1 for e in data['errors'] if e['level'] == 'WARN')
    log(f'  {len(data["reports"])} reports, {len(data["downloads"])} downloads')
    log(f'  {n_err} errors, {n_warn} warnings')

    cats = defaultdict(int)
    for e in data['errors']:
        cats[e['category']] += 1
    for cat, cnt in sorted(cats.items(), key=lambda x: -x[1]):
        log(f'    {cat}: {cnt}')

    daily = extract_daily(data)
    log(f'  serie diaria: {len(daily["hours"])} horas UTC (días se agrupan por tz en el cliente)')

    out = os.path.join(output_dir, output_name)
    _write_report(out, html_report(data, daily, max_reports=max_reports,
                                    top_farms_n=top_farms_n, refresh_seconds=refresh_s))
    log(f'Report written to : {out} (+ .gz)')

    perf_name = cfg.get('output', 'perf_name', fallback='performance.html')
    daily_perf = extract_daily_perf(perf_data)
    log(f'  {perf_data["total_ops"]} timed ops, {len(perf_data["logins"])} logins')
    pout = os.path.join(output_dir, perf_name)
    _write_report(pout, perf_report(perf_data, daily_perf, refresh_seconds=refresh_s))
    log(f'Perf report   to : {pout} (+ .gz)')

    idx = os.path.join(output_dir, 'index.html')
    _write_report(idx, index_html([('Actividad', output_name), ('Rendimiento', perf_name)],
                                   refresh_seconds=refresh_s))
    log(f'Índice escrito   : {idx} (+ .gz)')
    print(f'━━━ fin @ {generated_at_iso()} ━━━')

#!/usr/bin/env python3
"""Parse Scarab Precision log files and generate an HTML activity report."""

import configparser
import glob
import gzip
import html as html_mod
import json
import os
import re
from collections import defaultdict
from datetime import datetime

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
    events = []
    for path in sorted(glob.glob(os.path.join(log_dir, pattern)), reverse=True):
        events.extend(_parse_file(path))
    events.sort(key=lambda e: e['ts'])
    return events


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


def extract_activity(events):
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
        'hourly': dict(sorted(hourly.items())),
        'report_types': dict(report_types),
        'farms': dict(sorted(farms.items(), key=lambda x: -x[1])),
        'total_events': len(events),
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


def extract_performance(events):
    ops, logins, http_by_hour = _scan_events(events)
    agg = _aggregate(ops, logins, http_by_hour)
    return {
        'ops':       ops,
        'slow_ops':  sorted(ops, key=lambda x: -x['ms'])[:25],
        'logins':    logins,
        'total_ops': len(ops),
        **agg,
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

def perf_report(perf_data):
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
<meta http-equiv="refresh" content="300">
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
</style>
</head>
<body>
<header>
  <div>
    <h1>Scarab Precision — Dashboard de Rendimiento</h1>
    <small>{perf_data["total_ops"]:,} operaciones analizadas &nbsp;|&nbsp;
      <nav style="display:inline">
        <a href="activity.html">← Ver Actividad</a>
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

  <div class="charts-grid">
    <div class="chart-card wide">
      <h2>Tiempos de Respuesta por Hora — Promedio · P90 · P95</h2>
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
      {{ label: 'Promedio', data: H_AVG, borderColor: '#3b82f6', backgroundColor: 'transparent', tension: 0.3, pointRadius: 3 }},
      {{ label: 'P90',      data: H_P90, borderColor: '#f59e0b', backgroundColor: 'transparent', tension: 0.3, pointRadius: 3 }},
      {{ label: 'P95',      data: H_P95, borderColor: '#ef4444', backgroundColor: 'transparent', tension: 0.3, pointRadius: 3 }},
      {{ label: 'Máximo',   data: H_MAX, borderColor: '#94a3b8', backgroundColor: 'transparent', tension: 0.3, pointRadius: 2, borderDash: [4,3] }},
    ]
  }},
  options: {{ responsive: true, plugins: {{ legend: {{ position: 'top' }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'ms' }} }} }} }}
}});

const volChart = new Chart(document.getElementById('volChart'), {{
  type: 'bar',
  data: {{
    labels: H_ISO.map(i => fmtHour(i, currentTz)),
    datasets: [{{ label: 'Operaciones', data: H_CNT, backgroundColor: '#93c5fd' }}]
  }},
  options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'Operaciones' }} }} }} }}
}});

const netChart = new Chart(document.getElementById('netChart'), {{
  type: 'bar',
  data: {{
    labels: NET_ISO.map(i => fmtHour(i, currentTz)),
    datasets: [{{ label: 'Llamadas HTTP', data: NET_CNT, backgroundColor: '#c4b5fd' }}]
  }},
  options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'Llamadas' }} }} }} }}
}});

const typeChart = new Chart(document.getElementById('typeChart'), {{
  type: 'bar',
  data: {{
    labels: T_NAMES,
    datasets: [
      {{ label: 'Promedio (ms)', data: T_AVG, backgroundColor: '#3b82f6' }},
      {{ label: 'P95 (ms)',      data: T_P95, backgroundColor: '#fca5a5' }},
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
    datasets: [{{ label: 'ms / etiqueta', data: F_VALS, backgroundColor: '#fcd34d' }}]
  }},
  options: {{ responsive: true, indexAxis: 'y',
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ title: {{ display: true, text: 'ms/etiq' }} }} }} }}
}});

const loginChart = new Chart(document.getElementById('loginChart'), {{
  type: 'bar',
  data: {{
    labels: LH_ISO.map(i => fmtHour(i, currentTz)),
    datasets: [{{ label: 'Logins', data: LH_CNT, backgroundColor: '#6ee7b7' }}]
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
</script>
</body>
</html>'''


# ── HTML rendering ─────────────────────────────────────────────────────────────

CATEGORY_META = {
    'JWT_EXPIRED':     ('JWT expirado',          '#92400e', '#fef3c7', '#f59e0b'),
    'DB_DUPLICATE_KEY':('Clave duplicada en DB', '#991b1b', '#fee2e2', '#ef4444'),
    'DB_TX_ABORTED':   ('TX abortada (cascada)', '#7c3aed', '#ede9fe', '#8b5cf6'),
    'USER_NOT_FOUND':  ('Usuario no encontrado', '#1e40af', '#dbeafe', '#3b82f6'),
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

    copy_js = '''<script>
document.querySelectorAll('.copy-btn').forEach(btn => {
  btn.addEventListener('click', e => {
    e.stopPropagation();
    const pre = document.getElementById(btn.dataset.target);
    navigator.clipboard.writeText(pre.textContent).then(() => {
      btn.textContent = 'Copiado!';
      setTimeout(() => btn.textContent = 'Copiar', 1800);
    });
  });
});
</script>'''

    return summary_html + cats_html + copy_js


def html_report(data, max_reports=60, top_farms_n=60):
    hourly = data['hourly']
    hours, counts = list(hourly.keys()), list(hourly.values())
    max_count = max(counts) if counts else 1

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
<meta http-equiv="refresh" content="300">
<title>Scarab Precision — Actividad</title>
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
  .scrollable{{max-height:480px;overflow-y:auto}}
  /* ── Timezone selector ── */
  .tz-select{{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:6px;padding:4px 10px;font-size:.82rem;cursor:pointer;outline:none}}
  .tz-select:hover{{background:rgba(255,255,255,.25)}}
  .tz-select option{{background:#1a2744;color:#fff}}
</style>
</head>
<body>
<header style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
  <div>
    <h1>Scarab Precision — Log de Actividad</h1>
    <small>precision-8443.log &nbsp;|&nbsp;
      Último reporte: <span class="ts-hms" data-utc="{last_event_iso}">{last_event}</span>
      &nbsp;|&nbsp; {data["total_events"]:,} líneas procesadas
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
    <h2>Actividad por hora ({len(hourly)} horas)</h2>
    <div class="scrollable">
      <table>
        <thead><tr><th>Hora</th><th>Volumen</th><th>#</th></tr></thead>
        <tbody>{bar_rows}</tbody>
      </table>
    </div>
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
</body>
</html>'''


# ── Entry point ────────────────────────────────────────────────────────────────

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
    quiet       = cfg.getboolean('display', 'quiet',   fallback=False)

    def log(msg):
        if not quiet:
            print(msg)

    log(f'Log dir : {log_dir}')
    log(f'Pattern : {pattern}')

    events = parse_logs(log_dir, pattern)
    log(f'  {len(events)} log entries loaded')

    data = extract_activity(events)
    n_err  = sum(1 for e in data['errors'] if e['level'] == 'ERROR')
    n_warn = sum(1 for e in data['errors'] if e['level'] == 'WARN')
    log(f'  {len(data["reports"])} reports, {len(data["downloads"])} downloads')
    log(f'  {n_err} errors, {n_warn} warnings')

    cats = defaultdict(int)
    for e in data['errors']:
        cats[e['category']] += 1
    for cat, cnt in sorted(cats.items(), key=lambda x: -x[1]):
        log(f'    {cat}: {cnt}')

    out = os.path.join(output_dir, output_name)
    tmp = out + '.tmp'
    with open(tmp, 'w') as f:
        f.write(html_report(data, max_reports=max_reports, top_farms_n=top_farms_n))
    os.replace(tmp, out)
    log(f'Report written to : {out}')

    perf_name = cfg.get('output', 'perf_name', fallback='performance.html')
    perf_data = extract_performance(events)
    log(f'  {perf_data["total_ops"]} timed ops, {len(perf_data["logins"])} logins')
    pout = os.path.join(output_dir, perf_name)
    tmp  = pout + '.tmp'
    with open(tmp, 'w') as f:
        f.write(perf_report(perf_data))
    os.replace(tmp, pout)
    log(f'Perf report   to : {pout}')

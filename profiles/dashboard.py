"""Dashboard plugin.

Dominio distinto a precision: actividad dominada por consultas SQL (MyBatis),
logins ok/fallidos y errores SQL. Reusa el motor (parsing+caché) y el render
declarativo compartido; aporta su propia extracción y layout.
"""

import re
from collections import defaultdict

import logreport
from logreport.render import render_page
from profiles import common

NAME = 'dashboard'

_PREPARING = re.compile(r'Preparing:')

# Reglas de error: SQL primero, luego el sondeo común; lo demás cae en OTHER.
ERROR_RULES = [
    ('SQL_ERROR', 'Error SQL', '#991b1b', '#fee2e2', '#ef4444',
     lambda e, full: 'BadSqlGrammarException' in full or 'SQLException' in full),
    common.UNAUTH_PROBE_RULE,
]

_METRIC_KEYS = ['queries', 'logins_ok', 'logins_fail', 'errors_app', 'errors_probe']


# ── extracción (hooks del motor) ────────────────────────────────────────────────
def accumulate(events):
    hourly = defaultdict(int)
    metrics = {k: defaultdict(int) for k in _METRIC_KEYS}
    errors = []
    for e in events:
        ts, msg = e['ts'], e['msg']
        hk = ts.strftime('%Y-%m-%d %H')
        hourly[hk] += 1
        if _PREPARING.search(msg):
            metrics['queries'][hk] += 1
        if common.LOGIN_OK.search(msg):
            metrics['logins_ok'][hk] += 1
        elif common.LOGIN_FAIL.search(msg):
            metrics['logins_fail'][hk] += 1
        if e['level'] in ('WARN', 'ERROR'):
            cat = common.categorize_error(e, ERROR_RULES)
            errors.append({'ts': ts, 'level': e['level'], 'logger': e['logger'], **cat})
            key = 'errors_probe' if cat['category'] == 'UNAUTH_PROBE' else 'errors_app'
            metrics[key][hk] += 1
    return {'hourly': dict(hourly),
            'metrics': {k: dict(v) for k, v in metrics.items()},
            'errors': errors, 'total_events': len(events)}


def scan(events):
    return [], [], {}  # dashboard no registra operaciones cronometradas


def merge_activity(a, b):
    return {
        'hourly': logreport.merge_counts(a['hourly'], b['hourly']),
        'metrics': {k: logreport.merge_counts(a['metrics'][k], b['metrics'][k]) for k in _METRIC_KEYS},
        'errors': a['errors'] + b['errors'],
        'total_events': a['total_events'] + b['total_events'],
    }


def finalize_activity(raw):
    hour_keys = set(raw['hourly'])
    for k in _METRIC_KEYS:
        hour_keys |= set(raw['metrics'][k])
    hours = sorted(hour_keys)
    iso = lambda h: f'{h[:10]}T{h[11:13]}:00:00Z'
    series = {'hours': [iso(h) for h in hours],
              'vol': [raw['hourly'].get(h, 0) for h in hours]}
    for k in _METRIC_KEYS:
        series[k] = [raw['metrics'][k].get(h, 0) for h in hours]
    totals = {k: sum(raw['metrics'][k].values()) for k in _METRIC_KEYS}
    return {'series': series, 'totals': totals,
            'errors': raw['errors'], 'total_events': raw['total_events']}


def perf_from_scan(ops, logins, http_by_hour):
    return {'total_ops': len(ops)}


PROFILE = logreport.Profile(accumulate, scan, merge_activity, finalize_activity, perf_from_scan)


# ── render (layout declarativo sobre los componentes compartidos) ───────────────
def render_activity(data, cfg):
    t = data['totals']
    cards = [
        {'label': 'Líneas', 'value': f'{data["total_events"]:,}'},
        {'label': 'Queries SQL', 'value': f'{t["queries"]:,}'},
        {'label': 'Logins OK', 'value': t['logins_ok'], 'cls': 'green'},
        {'label': 'Logins fallidos', 'value': t['logins_fail'], 'cls': 'amber'},
        {'label': 'Errores app', 'value': t['errors_app'], 'cls': 'red'},
        {'label': 'Sondeo', 'value': t['errors_probe'], 'cls': 'amber'},
    ]
    daily = {'series': data['series'], 'metrics': [
        {'key': 'queries', 'label': 'Queries', 'cls': ''},
        {'key': 'logins_ok', 'label': 'Logins OK', 'cls': 'green'},
        {'key': 'logins_fail', 'label': 'Logins fallidos', 'cls': 'amber'},
        {'key': 'errors_app', 'label': 'Errores app', 'cls': 'red'},
        {'key': 'errors_probe', 'label': 'Sondeo', 'cls': 'amber'},
    ]}
    errors = {'items': data['errors'], 'meta': common.category_meta(ERROR_RULES)}
    return render_page(title='Scarab Dashboard — Actividad',
                       subtitle=f'dashboard-8443.log · {data["total_events"]:,} líneas procesadas',
                       refresh_seconds=cfg.refresh, cards=cards, daily=daily, errors=errors)


def render_perf(perf_data, cfg):
    return render_page(title='Scarab Dashboard — Rendimiento',
                       subtitle='Sin operaciones cronometradas en estos logs',
                       refresh_seconds=cfg.refresh,
                       cards=[{'label': 'Ops cronometradas', 'value': perf_data['total_ops']}])

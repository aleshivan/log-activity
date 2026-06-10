"""Dashboard plugin.

Dominio distinto a precision: actividad dominada por consultas SQL (MyBatis),
logins ok/fallidos y errores SQL. No hay latencias en ms, pero sí señales de
rendimiento propias: volumen de queries, top consultas por mapper.método, y el
"peso" de las consultas (filas devueltas por query). Reusa el motor (parsing +
caché) y el render declarativo compartido (cards, vista diaria, gráficas, errores).
"""

import re
from collections import Counter, defaultdict

import logreport
from logreport.render import render_page
from profiles import common

NAME = 'dashboard'
TITLE = 'Scarab Dashboard'

_PREPARING = re.compile(r'Preparing:')
_TOTAL     = re.compile(r'<==\s+Total:\s+(\d+)')
_COUNTRIES = ('colombia', 'ecuador', 'east_africa', 'mexico', 'peru')

ERROR_RULES = [
    ('SQL_ERROR', 'Error SQL', '#991b1b', '#fee2e2', '#ef4444',
     lambda e, full: 'BadSqlGrammarException' in full or 'SQLException' in full),
    common.UNAUTH_PROBE_RULE,
]

_METRIC_KEYS = ['queries', 'logins_ok', 'logins_fail', 'errors_app', 'errors_probe']
_TOP_QUERIES = 15


# ── extracción ──────────────────────────────────────────────────────────────────
def accumulate(events):
    hourly = defaultdict(int)
    metrics = {k: defaultdict(int) for k in _METRIC_KEYS}
    errors = []
    queries = Counter()
    countries = Counter()
    rows = []
    for e in events:
        ts, msg = e['ts'], e['msg']
        hk = ts.strftime('%Y-%m-%d %H')
        hourly[hk] += 1
        if _PREPARING.search(msg):
            metrics['queries'][hk] += 1
            queries[e['logger']] += 1
        if common.LOGIN_OK.search(msg):
            metrics['logins_ok'][hk] += 1
        elif common.LOGIN_FAIL.search(msg):
            metrics['logins_fail'][hk] += 1
        if 'Row:' in msg:
            for c in _COUNTRIES:
                if c in msg:
                    countries[c] += 1
        m = _TOTAL.search(msg)
        if m:
            rows.append(int(m.group(1)))
        if e['level'] in ('WARN', 'ERROR'):
            cat = common.categorize_error(e, ERROR_RULES)
            errors.append({'ts': ts, 'level': e['level'], 'logger': e['logger'], **cat})
            key = 'errors_probe' if cat['category'] == 'UNAUTH_PROBE' else 'errors_app'
            metrics[key][hk] += 1
    return {'hourly': dict(hourly),
            'metrics': {k: dict(v) for k, v in metrics.items()},
            'errors': errors, 'queries': dict(queries),
            'countries': dict(countries), 'rows': rows, 'total_events': len(events)}


def scan(events):
    return [], [], {}


def merge_activity(a, b):
    mc = logreport.merge_counts
    return {
        'hourly': mc(a['hourly'], b['hourly']),
        'metrics': {k: mc(a['metrics'][k], b['metrics'][k]) for k in _METRIC_KEYS},
        'errors': a['errors'] + b['errors'],
        'queries': mc(a['queries'], b['queries']),
        'countries': mc(a['countries'], b['countries']),
        'rows': a['rows'] + b['rows'],
        'total_events': a['total_events'] + b['total_events'],
    }


def _rows_hist(rows):
    h = {'0–10': 0, '11–50': 0, '51–200': 0, '201–1000': 0, '1000+': 0}
    for v in rows:
        if v <= 10:    h['0–10'] += 1
        elif v <= 50:  h['11–50'] += 1
        elif v <= 200: h['51–200'] += 1
        elif v <= 1000: h['201–1000'] += 1
        else:          h['1000+'] += 1
    return h


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
    return {
        'series': series,
        'totals': {k: sum(raw['metrics'][k].values()) for k in _METRIC_KEYS},
        'errors': raw['errors'],
        'total_events': raw['total_events'],
        'top_queries': sorted(raw['queries'].items(), key=lambda x: -x[1])[:_TOP_QUERIES],
        'countries': sorted(raw['countries'].items(), key=lambda x: -x[1]),
        'rows_stats': logreport.stats(raw['rows']),
        'rows_hist': _rows_hist(raw['rows']),
    }


def perf_from_scan(ops, logins, http_by_hour):
    return {'total_ops': len(ops)}


# ── render ────────────────────────────────────────────────────────────────────
def _top_queries_chart(data):
    top = data['top_queries']
    return {'id': 'qChart', 'section': 'Top consultas (mapper.método)', 'horizontal': True,
            'labels': [q[0] for q in top],
            'datasets': [{'label': 'Ejecuciones', 'data': [q[1] for q in top],
                          'backgroundColor': '#93c5fd'}],
            'xlabel': 'ejecuciones'}


def _country_chart(data):
    c = data['countries']
    return {'id': 'cChart', 'section': 'Actividad por país (filas en resultados)',
            'labels': [x[0] for x in c],
            'datasets': [{'label': 'Filas', 'data': [x[1] for x in c],
                          'backgroundColor': '#6ee7b7'}],
            'ylabel': 'filas'}


def render_activity(data, perf_data, cfg):
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
    charts = [_top_queries_chart(data), _country_chart(data)]
    errors = {'items': data['errors'], 'meta': common.category_meta(ERROR_RULES)}
    return render_page(title='Scarab Dashboard — Actividad',
                       subtitle=f'dashboard-8443.log · {data["total_events"]:,} líneas procesadas',
                       refresh_seconds=cfg.refresh, cards=cards, daily=daily,
                       charts=charts, errors=errors)


def render_perf(data, perf_data, cfg):
    rs = data['rows_stats']
    cards = [
        {'label': 'Queries SQL', 'value': f'{data["totals"]["queries"]:,}'},
        {'label': 'Filas/query p50', 'value': rs['p50']},
        {'label': 'Filas/query p90', 'value': rs['p90'], 'cls': 'amber'},
        {'label': 'Filas/query p95', 'value': rs['p95'], 'cls': 'red'},
        {'label': 'Máx filas', 'value': f'{rs["max"]:,}', 'cls': 'red'},
    ]
    hist = data['rows_hist']
    charts = [
        {'id': 'histChart', 'section': 'Distribución de filas por consulta',
         'labels': list(hist.keys()),
         'datasets': [{'label': 'Consultas', 'data': list(hist.values()),
                       'backgroundColor': '#c4b5fd'}],
         'xlabel': 'filas devueltas', 'ylabel': 'nº de consultas'},
        _top_queries_chart(data),
    ]
    tables = [{
        'title': 'Top consultas por volumen',
        'columns': ['Mapper.método', 'Ejecuciones'],
        'rows': [[logreport.esc(q[0]), f'<span class="num-cell">{q[1]:,}</span>']
                 for q in data['top_queries']],
    }]
    return render_page(title='Scarab Dashboard — Rendimiento',
                       subtitle='Throughput de consultas y peso de payload (sin latencias en ms)',
                       refresh_seconds=cfg.refresh, cards=cards, charts=charts, tables=tables)


PROFILE = logreport.Profile(accumulate, scan, merge_activity, finalize_activity, perf_from_scan)

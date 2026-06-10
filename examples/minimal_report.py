#!/usr/bin/env python3
"""Ejemplo mínimo: un proyecto nuevo montado sobre el motor `logreport`.

Reutiliza gratis: parseo de logs Spring Boot + caché incremental de .gz + stats.
Tú solo aportas la EXTRACCIÓN (qué contar) y el RENDER (aquí, un resumen de texto).

Uso:
    python examples/minimal_report.py [LOG_DIR] [PATRON]
    # p.ej.: python examples/minimal_report.py log 'precision-8443.log*'
"""

import os
import sys
from collections import defaultdict

# En un proyecto real: `pip install` el motor. Aquí, añade la raíz del repo al path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logreport


# ── Extracción (los 5 hooks que el motor necesita) ──────────────────────────────
def accumulate(events):
    """Agregados crudos y MERGEABLES (contadores, listas)."""
    by_level = defaultdict(int)
    hourly = defaultdict(int)
    for e in events:
        by_level[e['level']] += 1
        hourly[e['ts'].strftime('%Y-%m-%d %H:00')] += 1
    return {'total': len(events), 'by_level': dict(by_level), 'hourly': dict(hourly)}


def scan(events):
    """(ops_con_latencia, logins, http_por_hora). Este ejemplo no mide latencias."""
    return [], [], {}


def merge_activity(a, b):
    return {
        'total': a['total'] + b['total'],
        'by_level': logreport.merge_counts(a['by_level'], b['by_level']),
        'hourly': logreport.merge_counts(a['hourly'], b['hourly']),
    }


def finalize_activity(raw):
    return raw  # nada que ordenar en este ejemplo


def perf_from_scan(ops, logins, http_by_hour):
    return {'total_ops': len(ops)}


PROFILE = logreport.Profile(accumulate, scan, merge_activity, finalize_activity, perf_from_scan)


if __name__ == '__main__':
    log_dir = sys.argv[1] if len(sys.argv) > 1 else 'log'
    pattern = sys.argv[2] if len(sys.argv) > 2 else '*.log*'
    data, _perf = logreport.build_report_data(
        PROFILE, log_dir, pattern, cache_path='out/.example_cache.pkl', log=print)
    print(f'\nTotal de eventos: {data["total"]:,}')
    print('Por nivel:')
    for lvl, n in sorted(data['by_level'].items(), key=lambda x: -x[1]):
        print(f'  {lvl:6} {n:,}')
    print(f'Horas activas: {len(data["hourly"])}')

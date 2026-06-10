#!/usr/bin/env python3
"""CLI para generar reportes por sistema (plugin) y la página índice.

    python run.py precision     # genera el reporte de precision
    python run.py dashboard     # genera el reporte de dashboard
    python run.py index         # (re)genera index.html que enlaza todos
    python run.py all           # genera todos los perfiles + el índice

Cada perfil vive en profiles/<nombre>.py y usa profiles/<nombre>.config
(precision cae a activity.config si no tiene el suyo, por compatibilidad).
"""

import importlib
import os
import sys

from logreport.output import generated_at_iso, write_report
from logreport.render import render_index
from logreport.runner import load_config, run_plugin

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_NAMES = ['precision', 'precision_adm', 'precision_mbl', 'dashboard']


def _config_path(name):
    p = os.path.join(HERE, 'profiles', f'{name}.config')
    return p if os.path.exists(p) else os.path.join(HERE, 'activity.config')


def _load_plugin(name):
    return importlib.import_module(f'profiles.{name}')


def build_index():
    """Arma index.html enlazando Actividad/Rendimiento de cada perfil."""
    systems, out_dir = [], None
    for name in PROFILE_NAMES:
        try:
            plugin = _load_plugin(name)
        except ModuleNotFoundError:
            continue
        cfg = load_config(_config_path(name))
        out_dir = out_dir or cfg.output_dir
        systems.append({
            'title': getattr(plugin, 'TITLE', name),
            'links': [('Actividad', cfg.output_name), ('Rendimiento', cfg.perf_name)],
        })
    if not systems:
        print('No hay perfiles para indexar.')
        return
    html = render_index(systems, title='Scarab — Reportes',
                        subtitle='Monitoreo de actividad y rendimiento por sistema')
    out = os.path.join(out_dir, 'index.html')
    write_report(out, html)
    print(f'Índice escrito en : {out} (+ .gz)')


def main():
    if len(sys.argv) != 2:
        print('Uso: python run.py <precision|dashboard|index|all>')
        sys.exit(2)
    cmd = sys.argv[1]

    # Logstamp del run en ISO 8601 UTC (mismo instante que el sello de los reportes,
    # útil al redirigir la salida a un log bajo cron).
    stamp = generated_at_iso()
    print(f'━━━ run {cmd} @ {stamp} ━━━')

    if cmd == 'index':
        build_index()
    elif cmd == 'all':
        for name in PROFILE_NAMES:
            try:
                run_plugin(_load_plugin(name), _config_path(name))
            except ModuleNotFoundError:
                print(f'(omitido: falta profiles/{name}.py)')
        build_index()
    else:
        try:
            plugin = _load_plugin(cmd)
        except ModuleNotFoundError:
            print(f'Perfil desconocido: {cmd} (falta profiles/{cmd}.py)')
            sys.exit(2)
        run_plugin(plugin, _config_path(cmd))
        build_index()  # mantener el índice al día tras cada generación

    print(f'━━━ fin @ {stamp} ━━━')


if __name__ == '__main__':
    main()

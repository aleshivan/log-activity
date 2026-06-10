#!/usr/bin/env python3
"""CLI para generar el reporte de un sistema (plugin).

    python run.py precision
    python run.py dashboard

Cada perfil vive en profiles/<nombre>.py y usa profiles/<nombre>.config
(precision cae a activity.config si no tiene el suyo, por compatibilidad).
"""

import importlib
import os
import sys

from logreport.runner import run_plugin

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    if len(sys.argv) != 2:
        print('Uso: python run.py <perfil>   (p.ej. precision | dashboard)')
        sys.exit(2)
    name = sys.argv[1]
    try:
        plugin = importlib.import_module(f'profiles.{name}')
    except ModuleNotFoundError:
        print(f'Perfil desconocido: {name} (falta profiles/{name}.py)')
        sys.exit(2)

    cfg_path = os.path.join(HERE, 'profiles', f'{name}.config')
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(HERE, 'activity.config')  # compat precision
    run_plugin(plugin, cfg_path)


if __name__ == '__main__':
    main()

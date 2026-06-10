"""Generic orchestration: load a plugin's config, build the report data (reusing
the incremental cache), render via the plugin, and write HTML + .gz.

A *plugin* is a module exposing:
    NAME            : str
    PROFILE         : logreport.Profile     (extraction hooks)
    render_activity(data, cfg) -> html str
    render_perf(perf_data, cfg) -> html str
"""

import configparser
import os
from types import SimpleNamespace

from .cache import build_report_data
from .output import write_report


def load_config(cfg_path):
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(f'Config file not found: {cfg_path}')
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    return SimpleNamespace(
        log_dir     = cfg.get('logs',    'dir'),
        pattern     = cfg.get('logs',    'pattern'),
        output_dir  = cfg.get('output',  'dir'),
        output_name = cfg.get('output',  'name',        fallback='activity.html'),
        perf_name   = cfg.get('output',  'perf_name',   fallback='performance.html'),
        max_reports = cfg.getint('display', 'max_reports', fallback=60),
        top_farms   = cfg.getint('display', 'top_farms',   fallback=60),
        refresh     = cfg.getint('display', 'refresh_seconds', fallback=1200),
        quiet       = cfg.getboolean('display', 'quiet',   fallback=False),
    )


def run_plugin(plugin, cfg_path):
    cfg = load_config(cfg_path)

    def log(msg):
        if not cfg.quiet:
            print(msg)

    log(f'Perfil  : {plugin.NAME}')
    log(f'Log dir : {cfg.log_dir}')
    log(f'Pattern : {cfg.pattern}')

    cache_path = os.path.join(cfg.output_dir, f'.report_cache.{plugin.NAME}.pkl')
    data, perf_data = build_report_data(plugin.PROFILE, cfg.log_dir, cfg.pattern, cache_path, log)
    log(f'  {data["total_events"]:,} log entries loaded')

    out = os.path.join(cfg.output_dir, cfg.output_name)
    write_report(out, plugin.render_activity(data, cfg))
    log(f'Report written to : {out} (+ .gz)')

    pout = os.path.join(cfg.output_dir, cfg.perf_name)
    write_report(pout, plugin.render_perf(perf_data, cfg))
    log(f'Perf report   to : {pout} (+ .gz)')

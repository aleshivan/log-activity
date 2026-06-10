"""Precision plugin (y variantes por instancia: adm, mbl).

Reuses the extraction + render that already live in activity.py (the original
Scarab report), exposed as a plugin for the generic runner. Output is identical
to running activity.py directly.

Las instancias adm/móvil comparten exactamente este formato de log y render; sólo
cambian el patrón de archivos, los nombres de salida y la etiqueta visible. Por
eso reutilizan el mismo PROFILE y los factories make_render_* de aquí.
"""

import activity
import logreport

NAME = 'precision'
TITLE = 'Scarab Precision'

PROFILE = logreport.Profile(
    accumulate=activity._accumulate_activity,
    scan=activity._scan_events,
    merge_activity=activity._merge_activity,
    finalize_activity=activity._finalize_activity,
    perf_from_scan=activity._perf_from_scan,
)


def _log_name(cfg):
    """Nombre de archivo a mostrar, derivado del patrón (sin el comodín)."""
    return cfg.pattern.rstrip('*')


def make_render_activity(label):
    def render_activity(data, perf_data, cfg):
        daily = activity.extract_daily(data)
        return activity.html_report(data, daily, max_reports=cfg.max_reports,
                                    top_farms_n=cfg.top_farms, refresh_seconds=cfg.refresh,
                                    label=label, log_name=_log_name(cfg),
                                    perf_name=cfg.perf_name)
    return render_activity


def make_render_perf(label):
    def render_perf(data, perf_data, cfg):
        daily_perf = activity.extract_daily_perf(perf_data)
        return activity.perf_report(perf_data, daily_perf, refresh_seconds=cfg.refresh,
                                    label=label, activity_name=cfg.output_name)
    return render_perf


render_activity = make_render_activity(TITLE)
render_perf = make_render_perf(TITLE)

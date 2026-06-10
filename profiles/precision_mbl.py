"""Instancia móvil de Scarab Precision (precision-mbl-9450).

Mismo formato de log y render que precision; sólo cambia patrón/salida/etiqueta
(ver profiles/precision_mbl.config). Reutiliza el PROFILE y los factories de render.
"""

from profiles.precision import PROFILE, make_render_activity, make_render_perf

NAME = 'precision_mbl'
TITLE = 'Scarab Precision · Móvil'

render_activity = make_render_activity(TITLE)
render_perf = make_render_perf(TITLE)

__all__ = ['NAME', 'TITLE', 'PROFILE', 'render_activity', 'render_perf']

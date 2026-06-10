"""Instancia de administración de Scarab Precision (precision-adm-9090).

Mismo formato de log y render que precision; sólo cambia patrón/salida/etiqueta
(ver profiles/precision_adm.config). Reutiliza el PROFILE y los factories de render.
"""

from profiles.precision import PROFILE, make_render_activity, make_render_perf

NAME = 'precision_adm'
TITLE = 'Scarab Precision · Admin'

render_activity = make_render_activity(TITLE)
render_perf = make_render_perf(TITLE)

__all__ = ['NAME', 'TITLE', 'PROFILE', 'render_activity', 'render_perf']

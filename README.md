# log-activity

Generador de reportes HTML de actividad y rendimiento a partir de logs (formato
Spring Boot). Construido sobre un **motor genérico reutilizable** (`logreport/`)
para poder usarlo en varios proyectos.

## Arquitectura

```
logreport/            # MOTOR genérico (agnóstico al dominio)
  parsing.py          #   formato Spring Boot -> eventos
  cache.py            #   caché incremental de .gz + build_report_data(profile, ...)
  stats.py            #   percentiles y merge de contadores
  output.py           #   escritura atómica + .gz (para gzip_static de nginx)
activity.py           # PERFIL Scarab + entry point (extracción + render HTML)
activity.config       # config del perfil Scarab (rutas, display)
examples/
  minimal_report.py   # ejemplo mínimo de un proyecto nuevo sobre el motor
```

El motor no sabe nada de "fincas", "reportes" ni de Scarab: eso vive en el perfil.

## Qué te da el motor gratis

- **Parseo** de logs Spring Boot (líneas multilinea/stack traces incluidas).
- **Caché incremental**: los `.gz` rotados (historia inmutable) se agregan una vez
  y se cachean; cada corrida solo reprocesa el log activo. Corridas ~5x más rápidas.
- **Stats**: percentiles (p50/p90/p95) y merge de contadores.
- **Salida `.gz`**: escribe `reporte.html` + `reporte.html.gz` para que nginx lo
  sirva con `gzip_static on` (transferencia mínima, sin CPU por petición).

## Cómo añadir un proyecto nuevo

1. Pon el motor en el path (`pip install` desde este repo, o copia `logreport/`).
2. Escribe un perfil con los 5 hooks de extracción y pásalo al motor:

```python
import logreport

def accumulate(events): ...        # agregados crudos MERGEABLES (contadores, listas)
def scan(events): ...              # (ops_con_latencia, logins, http_por_hora)
def merge_activity(a, b): ...      # combinar agregados (a precede a b en el tiempo)
def finalize_activity(raw): ...    # ordenar/cerrar el dict final para render
def perf_from_scan(ops, logins, http): ...

PROFILE = logreport.Profile(accumulate, scan, merge_activity, finalize_activity, perf_from_scan)
data, perf = logreport.build_report_data(PROFILE, log_dir, pattern,
                                         cache_path='out/.cache.pkl', log=print)
# ... renderiza data/perf a HTML y escribe con logreport.write_report(path, html)
```

Ver `examples/minimal_report.py` (resumen de texto) como punto de partida, y
`activity.py` como referencia completa (extracción rica + render HTML con vista
diaria comparable, paginación, lazy-stacks, gráficas Chart.js).

> **Nota:** el render HTML completo aún es específico de cada proyecto: hoy se
> parte de `activity.py` y se adapta. El siguiente paso de generalización es
> volver el render declarativo (métricas/tarjetas/gráficas por config). El motor
> de datos (parsing, caché, stats, salida) ya es totalmente reutilizable.

## Despliegue (perfil Scarab)

- Cron regenera los reportes; recomendado con baja prioridad:
  `*/20 * * * * nice -n 19 ionice -c3 python3 activity.py`
- nginx sirve `/report` con `gzip_static on; gunzip on;` (usa los `.html.gz`).
- `activity.config` `[display] refresh_seconds` controla la auto-recarga del navegador.

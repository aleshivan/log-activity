"""HTML output helpers: escaping and atomic write + precompressed .gz sibling."""

import datetime
import gzip
import html as html_mod
import os


def esc(s):
    return html_mod.escape(str(s))


_GENERATED_AT = None


def generated_at_iso():
    """Marca de generación del run en ISO 8601 UTC (p.ej. 2026-06-10T16:02:33Z).

    Se calcula una sola vez por proceso, así todos los reportes de un mismo
    `run.py` comparten exactamente el mismo instante.
    """
    global _GENERATED_AT
    if _GENERATED_AT is None:
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        _GENERATED_AT = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    return _GENERATED_AT


def write_report(path, content):
    """Write the HTML and a precompressed .gz sibling (atomically, each).

    nginx `gzip_static on` serves the .gz directly (Content-Encoding: gzip),
    so a large report transfers as a fraction of its size without per-request CPU.
    """
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(content)
    os.replace(tmp, path)

    gz_tmp = path + '.gz.tmp'
    with gzip.open(gz_tmp, 'wt', encoding='utf-8') as f:
        f.write(content)
    os.replace(gz_tmp, path + '.gz')

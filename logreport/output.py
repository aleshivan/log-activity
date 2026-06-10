"""HTML output helpers: escaping and atomic write + precompressed .gz sibling."""

import gzip
import html as html_mod
import os


def esc(s):
    return html_mod.escape(str(s))


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

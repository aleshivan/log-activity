"""Render declarativo compartido: a partir de bloques (cards, vista diaria,
tablas, sección de errores) arma un reporte HTML autocontenido con:
- selector de zona horaria (re-render de timestamps en cliente)
- vista diaria comparable (bucketing por tz en el cliente, overlay de comparación)
- sección de errores categorizada con paginación + stacks bajo demanda (lazy)
- salida lista para gzip_static

Los datos se embeben como JSON islands y el JS los lee, así el JS va como
constante plana (sin escapar llaves de f-string).
"""

import json

from .output import esc

TZ_OPTIONS = '''
  <option value="UTC">UTC</option>
  <option value="America/Bogota">GMT-5 — Colombia</option>
  <option value="America/Lima">GMT-5 — Perú</option>
  <option value="America/New_York">GMT-5/-4 — New York</option>
  <option value="America/Mexico_City">GMT-6/-5 — México</option>
  <option value="America/Santiago">GMT-4/-3 — Chile</option>
  <option value="Africa/Nairobi">GMT+3 — Nairobi</option>
  <option value="Europe/Madrid">GMT+1/+2 — Madrid</option>'''

CSS = '''
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f4f6f9;color:#333}
  header{background:#1a2744;color:#fff;padding:20px 32px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
  header h1{font-size:1.4rem;font-weight:600}
  header small{opacity:.7;font-size:.85rem}
  .tz-select{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:6px;padding:4px 10px;font-size:.82rem;cursor:pointer;outline:none}
  .tz-select option{background:#1a2744;color:#fff}
  .container{max-width:1200px;margin:24px auto;padding:0 16px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px;margin-bottom:24px}
  .card{background:#fff;border-radius:8px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.08);text-align:center}
  .card .num{font-size:2rem;font-weight:700;color:#1a2744}
  .card .num.red{color:#dc2626} .card .num.amber{color:#d97706} .card .num.green{color:#16a34a}
  .card .lbl{font-size:.78rem;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:.05em}
  .section{background:#fff;border-radius:8px;padding:20px 24px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:24px}
  .section h2{font-size:1rem;font-weight:600;margin-bottom:16px;color:#1a2744;border-bottom:2px solid #e8ecf0;padding-bottom:8px}
  table{width:100%;border-collapse:collapse;font-size:.875rem}
  th{text-align:left;padding:6px 10px;background:#f0f3f7;color:#555;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}
  td{padding:7px 10px;border-bottom:1px solid #f0f0f0;vertical-align:middle}
  tr:last-child td{border-bottom:none}
  .num-cell{text-align:right;font-family:monospace}
  .scrollable{max-height:480px;overflow-y:auto}
  .day-bar{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:16px}
  .day-nav{background:#1a2744;color:#fff;border:none;border-radius:6px;width:32px;height:32px;font-size:1.1rem;cursor:pointer;line-height:1}
  .day-nav:hover{background:#2c3e63} .day-nav:disabled{opacity:.3;cursor:default}
  .day-sel{border:1px solid #cbd5e1;border-radius:6px;padding:5px 10px;font-size:.9rem;cursor:pointer;background:#fff}
  .day-cmp-lbl{font-size:.82rem;color:#666;margin-left:8px}
  .day-badge{display:inline-block;background:#fef3c7;color:#92400e;border-radius:12px;padding:2px 10px;font-size:.72rem;font-weight:600}
  .day-note{font-size:.78rem;color:#888;margin:10px 0 0}
  .day-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:18px}
  .day-card{background:#f8fafc;border:1px solid #e8ecf0;border-radius:8px;padding:14px;text-align:center}
  .day-card .num{font-size:1.7rem;font-weight:700;color:#1a2744}
  .day-card .lbl{font-size:.72rem;color:#666;margin-top:3px;text-transform:uppercase;letter-spacing:.04em}
  .day-card .delta{font-size:.78rem;margin-top:5px;font-weight:600;min-height:1em}
  .delta.up{color:#dc2626} .delta.down{color:#16a34a} .delta.flat{color:#9ca3af}
  .day-chart-wrap{position:relative;height:300px}
  .err-summary{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
  .err-pill{display:inline-block;padding:5px 14px;border-radius:20px;font-size:.82rem;text-decoration:none}
  .err-cat{border:1px solid #e5e7eb;border-radius:8px;margin-bottom:14px;overflow:hidden}
  .err-cat-header{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;font-size:.9rem}
  .err-cat-label{font-weight:600} .err-cat-count{font-size:.8rem;opacity:.8}
  .err-cat-body{padding:0 12px 8px}
  .err-item{border-bottom:1px solid #f3f4f6;padding:10px 4px}
  .err-item:last-child{border-bottom:none}
  .err-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px}
  .err-ts{font-family:monospace;font-size:.78rem;color:#6b7280}
  .err-badge{font-size:.72rem;font-weight:700;padding:1px 7px;border-radius:3px}
  .err-error{background:#fee2e2;color:#991b1b} .err-warn{background:#fef3c7;color:#92400e}
  .err-logger{font-family:monospace;font-size:.75rem;color:#4b5563}
  .err-summary-line{font-size:.875rem;color:#111;margin-bottom:2px}
  details summary{font-size:.78rem;color:#2563eb;cursor:pointer;margin-top:4px}
  .stack{font-family:monospace;font-size:.73rem;background:#1e1e2e;color:#cdd6f4;padding:14px;border-radius:6px;margin-top:8px;overflow-x:auto;white-space:pre;line-height:1.5}
  .err-pager{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 4px;border-bottom:1px solid #f0f0f0;margin-bottom:4px}
  .err-filter{flex:1;min-width:160px;border:1px solid #cbd5e1;border-radius:6px;padding:5px 10px;font-size:.82rem;outline:none}
  .pg-btn{background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe;border-radius:6px;padding:4px 10px;font-size:.8rem;cursor:pointer;font-weight:600}
  .pg-btn:disabled{opacity:.4;cursor:default}
  .pg-info{font-size:.8rem;color:#666}
'''

# JS: lee los JSON islands (#dailySeries, #dailyMetrics, #stackData) y arma todo.
JS = '''
(function () {
  var STORE = function (id, dflt) {
    var el = document.getElementById(id);
    return el ? JSON.parse(el.textContent) : dflt;
  };
  var TZ_KEY = 'logreport_tz', DAY_KEY = 'logreport_day';
  var pad = function (n) { return String(n).padStart(2, '0'); };
  var labels = Array.from({ length: 24 }, function (_, h) { return pad(h) + 'h'; });

  // ── selector de zona horaria (timestamps explícitos) ──
  function fmtHms(d, tz) {
    return new Intl.DateTimeFormat('es', { timeZone: tz, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(d);
  }
  function applyTz(tz) {
    document.querySelectorAll('.ts-hms[data-utc]').forEach(function (el) {
      el.textContent = fmtHms(new Date(el.dataset.utc), tz);
    });
    localStorage.setItem(TZ_KEY, tz);
  }

  // ── vista diaria comparable (bucketing por tz en cliente) ──
  var SERIES = STORE('dailySeries', null), METRICS = STORE('dailyMetrics', []);
  var DAILY = {}, DAYS = [], chart = null;

  function localBucket(iso, tz) {
    var p = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(iso));
    var v = {}; p.forEach(function (x) { v[x.type] = x.value; });
    return [v.year + '-' + v.month + '-' + v.day, parseInt(v.hour, 10) % 24];
  }
  function buildDaily(tz) {
    var keys = ['vol'].concat(METRICS.map(function (m) { return m.key; }));
    var days = {};
    SERIES.hours.forEach(function (iso, i) {
      var b = localBucket(iso, tz), d = b[0], h = b[1];
      if (!days[d]) { days[d] = {}; keys.forEach(function (k) { days[d][k] = Array(24).fill(0); }); }
      keys.forEach(function (k) { days[d][k][h] += (SERIES[k] ? SERIES[k][i] : 0); });
    });
    Object.keys(days).forEach(function (d) {
      var rec = days[d], active = [];
      for (var h = 0; h < 24; h++) if (rec.vol[h] > 0) active.push(h);
      rec.first_hour = active.length ? active[0] : 0;
      rec.last_hour = active.length ? active[active.length - 1] : 23;
      rec.partial = rec.first_hour > 0 || rec.last_hour < 23;
    });
    return days;
  }
  var sum = function (a, lo, hi) { var t = 0; for (var h = lo; h <= hi; h++) t += a[h] || 0; return t; };

  function buildCards() {
    var box = document.getElementById('dailyCards');
    if (!box) return;
    box.innerHTML = METRICS.map(function (m) {
      return '<div class="day-card"><div class="num ' + (m.cls || '') + '" id="m_' + m.key + '">—</div>' +
             '<div class="lbl">' + m.label + '</div><div class="delta" id="d_' + m.key + '"></div></div>';
    }).join('');
  }
  function setDelta(key, cur, base) {
    var el = document.getElementById('d_' + key); if (!el) return;
    if (base == null) { el.textContent = ''; el.className = 'delta'; return; }
    if (base === 0) { el.textContent = cur > 0 ? '▲ nuevo' : '–'; el.className = 'delta ' + (cur > 0 ? 'up' : 'flat'); return; }
    var pct = Math.round((cur - base) / base * 100);
    var dir = pct > 0 ? 'up' : (pct < 0 ? 'down' : 'flat');
    el.textContent = (pct > 0 ? '▲ +' : (pct < 0 ? '▼ ' : '= ')) + pct + '%';
    el.className = 'delta ' + dir;
  }
  function renderDaily() {
    var daySel = document.getElementById('daySel'), cmpSel = document.getElementById('cmpSel');
    var day = daySel.value, cmp = cmpSel.value, D = DAILY[day];
    if (!D) return;
    var C = cmp && DAILY[cmp] ? DAILY[cmp] : null;
    document.getElementById('dayPartial').style.display = D.partial ? '' : 'none';
    var lo = D.first_hour, hi = D.last_hour, win = null;
    if (C) { lo = Math.max(D.first_hour, C.first_hour); hi = Math.min(D.last_hour, C.last_hour); win = lo <= hi ? [lo, hi] : null; }
    METRICS.forEach(function (m) {
      document.getElementById('m_' + m.key).textContent = sum(D[m.key], 0, 23).toLocaleString('es') + (D.partial ? ' *' : '');
      if (C && win) setDelta(m.key, sum(D[m.key], win[0], win[1]), sum(C[m.key], win[0], win[1]));
      else setDelta(m.key, 0, null);
    });
    var ds = [{ label: day, data: D.vol, backgroundColor: 'rgba(74,127,203,.65)', borderColor: '#4a7fcb', borderWidth: 1 }];
    if (C) ds.push({ label: cmp, data: C.vol, type: 'line', borderColor: '#e65100', backgroundColor: 'rgba(230,81,0,.1)', borderWidth: 2, pointRadius: 2, tension: .3, fill: false });
    if (chart) { chart.data.datasets = ds; chart.update(); }
    else chart = new Chart(document.getElementById('dayChart'), {
      type: 'bar', data: { labels: labels, datasets: ds },
      options: { responsive: true, maintainAspectRatio: false,
        scales: { x: { title: { display: true, text: 'hora del día (local)' } }, y: { beginAtZero: true, title: { display: true, text: 'eventos' } } },
        plugins: { legend: { position: 'top' } } }
    });
    var txt = 'Día ' + day + (D.partial ? ' (parcial *)' : '') + ' · franja ' + pad(D.first_hour) + '–' + pad(D.last_hour) + 'h (hora local).';
    if (C) txt += win ? ' Δ vs ' + cmp + ' sobre franja común ' + pad(win[0]) + '–' + pad(win[1]) + 'h.' : ' Sin franja común con ' + cmp + '.';
    document.getElementById('dayNote').textContent = txt;
    var i = DAYS.indexOf(day);
    document.getElementById('dayPrev').disabled = i <= 0;
    document.getElementById('dayNext').disabled = i >= DAYS.length - 1;
    localStorage.setItem(DAY_KEY, day);
  }
  function fillDaySelectors() {
    var daySel = document.getElementById('daySel'), cmpSel = document.getElementById('cmpSel');
    var cur = daySel.value, curCmp = cmpSel.value;
    daySel.innerHTML = ''; cmpSel.innerHTML = '<option value="">(ninguno)</option>';
    DAYS.forEach(function (d) {
      var star = DAILY[d].partial ? ' *' : '';
      daySel.insertAdjacentHTML('beforeend', '<option value="' + d + '">' + d + star + '</option>');
      cmpSel.insertAdjacentHTML('beforeend', '<option value="' + d + '">' + d + star + '</option>');
    });
    var saved = localStorage.getItem(DAY_KEY);
    daySel.value = (cur && DAILY[cur]) ? cur : (saved && DAILY[saved]) ? saved : DAYS[DAYS.length - 1];
    if (curCmp && DAILY[curCmp]) cmpSel.value = curCmp;
  }
  function rebuildDaily(tz) {
    if (!SERIES) return;
    DAILY = buildDaily(tz); DAYS = Object.keys(DAILY).sort();
    fillDaySelectors(); renderDaily();
  }

  // ── errores: stacks bajo demanda + paginación + filtro ──
  var STACKS = STORE('stackData', []);
  function fillStack(pre) {
    if (!pre || !pre.classList.contains('lazy')) return;
    pre.innerHTML = STACKS[+pre.dataset.sid]; pre.classList.remove('lazy');
  }
  document.querySelectorAll('details').forEach(function (d) {
    d.addEventListener('toggle', function () { if (d.open) d.querySelectorAll('pre.stack.lazy').forEach(fillStack); });
  });
  (function paginate() {
    var PAGE = 20;
    document.querySelectorAll('.err-cat').forEach(function (cat) {
      var body = cat.querySelector('.err-cat-body');
      if (!body) return;
      var items = Array.from(body.querySelectorAll(':scope > .err-item'));
      if (items.length <= PAGE) return;
      var texts = items.map(function (el) { return el.textContent.toLowerCase(); });
      var bar = document.createElement('div'); bar.className = 'err-pager';
      var filter = document.createElement('input'); filter.type = 'search'; filter.placeholder = 'Filtrar…'; filter.className = 'err-filter';
      var prev = document.createElement('button'); prev.textContent = '‹ Anterior'; prev.className = 'pg-btn';
      var next = document.createElement('button'); next.textContent = 'Siguiente ›'; next.className = 'pg-btn';
      var info = document.createElement('span'); info.className = 'pg-info';
      bar.append(filter, prev, info, next); body.parentNode.insertBefore(bar, body);
      var page = 0;
      function apply() {
        var q = filter.value.trim().toLowerCase();
        var filtered = q ? items.filter(function (_, i) { return texts[i].includes(q); }) : items;
        var pages = Math.max(1, Math.ceil(filtered.length / PAGE));
        if (page >= pages) page = pages - 1; if (page < 0) page = 0;
        items.forEach(function (el) { el.style.display = 'none'; });
        filtered.slice(page * PAGE, page * PAGE + PAGE).forEach(function (el) { el.style.display = ''; });
        info.textContent = 'Página ' + (page + 1) + ' / ' + pages + ' · ' + filtered.length + ' ítem(s)';
        prev.disabled = page <= 0; next.disabled = page >= pages - 1;
      }
      prev.addEventListener('click', function () { page--; apply(); });
      next.addEventListener('click', function () { page++; apply(); });
      filter.addEventListener('input', function () { page = 0; apply(); });
      apply();
    });
  })();

  // ── init ──
  var tz0 = localStorage.getItem(TZ_KEY) || 'UTC';
  var tzSel = document.getElementById('tzSelect');
  if (tzSel) {
    var opt = tzSel.querySelector('option[value="' + tz0 + '"]'); if (opt) opt.selected = true;
    tzSel.addEventListener('change', function () { applyTz(tzSel.value); rebuildDaily(tzSel.value); });
  }
  applyTz(tz0);
  if (SERIES) {
    buildCards();
    document.getElementById('dayPrev').addEventListener('click', function () { var i = DAYS.indexOf(document.getElementById('daySel').value) - 1; if (i >= 0) { document.getElementById('daySel').value = DAYS[i]; renderDaily(); } });
    document.getElementById('dayNext').addEventListener('click', function () { var i = DAYS.indexOf(document.getElementById('daySel').value) + 1; if (i < DAYS.length) { document.getElementById('daySel').value = DAYS[i]; renderDaily(); } });
    document.getElementById('daySel').addEventListener('change', renderDaily);
    document.getElementById('cmpSel').addEventListener('change', renderDaily);
    rebuildDaily(tz0);
  }
})();
'''


def _cards(cards):
    if not cards:
        return ''
    cells = ''.join(
        f'<div class="card"><div class="num {c.get("cls","")}">{esc(c["value"])}</div>'
        f'<div class="lbl">{esc(c["label"])}</div></div>'
        for c in cards
    )
    return f'<div class="cards">{cells}</div>'


def _daily(daily):
    if not daily:
        return ''
    return '''
  <div class="section">
    <h2>Vista diaria — comparar días</h2>
    <div class="day-bar">
      <button class="day-nav" id="dayPrev" title="Día anterior">‹</button>
      <select class="day-sel" id="daySel"></select>
      <button class="day-nav" id="dayNext" title="Día siguiente">›</button>
      <span class="day-badge" id="dayPartial" style="display:none">parcial</span>
      <span class="day-cmp-lbl">Comparar con:</span>
      <select class="day-sel" id="cmpSel"></select>
    </div>
    <div class="day-cards" id="dailyCards"></div>
    <div class="day-chart-wrap"><canvas id="dayChart"></canvas></div>
    <div class="day-note" id="dayNote"></div>
  </div>'''


def _table(t):
    head = ''.join(f'<th>{esc(c)}</th>' for c in t['columns'])
    body = ''.join(
        '<tr>' + ''.join(f'<td>{cell}</td>' for cell in row) + '</tr>'
        for row in t['rows']
    ) or f'<tr><td colspan="{len(t["columns"])}">Sin datos</td></tr>'
    return (f'<div class="section"><h2>{esc(t["title"])}</h2>'
            f'<div class="scrollable"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table></div></div>')


def _errors(errors):
    """errors = {'items': [...], 'meta': {cat: (label, fg, bg, border)}}; cada item
    trae ts, level, logger, summary, full, category. Devuelve (html, stack_store)."""
    if not errors or not errors['items']:
        return '<div class="section"><h2>Errores</h2><p style="color:#16a34a">Sin errores.</p></div>', []
    meta = errors['meta']
    by_cat = {}
    for e in errors['items']:
        by_cat.setdefault(e['category'], []).append(e)
    order = sorted(by_cat.items(), key=lambda x: -len(x[1]))

    pills = ''.join(
        f'<a href="#cat-{cat}" class="err-pill" style="background:{meta.get(cat, meta["OTHER"])[2]};'
        f'color:{meta.get(cat, meta["OTHER"])[1]};border:1px solid {meta.get(cat, meta["OTHER"])[3]}">'
        f'{esc(meta.get(cat, meta["OTHER"])[0])} <b>{len(items)}</b></a>'
        for cat, items in order
    )

    stack_store = []
    cats_html = ''
    for cat, items in order:
        label, fg, bg, border = meta.get(cat, meta['OTHER'])
        rows = ''
        for e in items:
            iso = e['ts'].strftime('%Y-%m-%dT%H:%M:%SZ')
            ts = e['ts'].strftime('%Y-%m-%d %H:%M:%S')
            stack_store.append(esc(e.get('full', '')))
            sid = len(stack_store) - 1
            rows += (
                '<div class="err-item">'
                f'<div class="err-meta"><span class="err-ts ts-hms" data-utc="{iso}">{esc(ts)}</span>'
                f'<span class="err-badge err-{e["level"].lower()}">{esc(e["level"])}</span>'
                f'<span class="err-logger">{esc(e["logger"])}</span></div>'
                f'<div class="err-summary-line">{esc(e["summary"])}</div>'
                f'<details><summary>Ver detalle</summary>'
                f'<pre class="stack lazy" data-sid="{sid}"></pre></details>'
                '</div>'
            )
        cats_html += (
            f'<div class="err-cat" id="cat-{cat}">'
            f'<div class="err-cat-header" style="background:{bg};border-left:4px solid {border};color:{fg}">'
            f'<span class="err-cat-label">{esc(label)}</span>'
            f'<span class="err-cat-count">{len(items)} ocurrencia(s)</span></div>'
            f'<div class="err-cat-body">{rows}</div></div>'
        )
    html = f'<div class="section"><h2>Errores y warnings — detalle</h2><div class="err-summary">{pills}</div>{cats_html}</div>'
    return html, stack_store


INDEX_CSS = '''
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f4f6f9;color:#333}
  header{background:#1a2744;color:#fff;padding:28px 32px}
  header h1{font-size:1.5rem;font-weight:600}
  header small{opacity:.7;font-size:.9rem}
  .container{max-width:900px;margin:32px auto;padding:0 16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px}
  .idx-card{background:#fff;border-radius:10px;padding:24px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
  .idx-card h2{font-size:1.1rem;color:#1a2744;margin-bottom:14px;border-bottom:2px solid #e8ecf0;padding-bottom:8px}
  .idx-links{display:flex;flex-direction:column;gap:8px}
  .idx-links a{display:flex;justify-content:space-between;align-items:center;text-decoration:none;
    background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe;border-radius:8px;padding:10px 14px;font-size:.92rem;font-weight:600}
  .idx-links a:hover{background:#c7d2fe}
  .idx-links a .arrow{opacity:.6}
'''


def render_index(systems, *, title='Reportes', subtitle='', refresh_seconds=1200, lang='es'):
    """Landing page que enlaza los reportes de cada sistema.

    systems = [{'title': str, 'links': [(label, href), ...]}]
    """
    cards = ''
    for s in systems:
        links = ''.join(
            f'<a href="{esc(href)}">{esc(label)}<span class="arrow">→</span></a>'
            for label, href in s['links']
        )
        cards += (f'<div class="idx-card"><h2>{esc(s["title"])}</h2>'
                  f'<div class="idx-links">{links}</div></div>')
    return (
        '<!DOCTYPE html>\n<html lang="' + lang + '">\n<head>\n<meta charset="UTF-8">\n'
        f'<meta http-equiv="refresh" content="{refresh_seconds}">\n'
        f'<title>{esc(title)}</title>\n<style>' + INDEX_CSS + '</style>\n</head>\n<body>\n'
        f'<header><h1>{esc(title)}</h1>'
        + (f'<small>{esc(subtitle)}</small>' if subtitle else '')
        + '</header>\n<div class="container">\n' + cards + '\n</div>\n</body>\n</html>'
    )


def render_page(*, title, subtitle='', refresh_seconds=1200, lang='es',
                cards=None, daily=None, tables=None, errors=None):
    """Arma el reporte HTML completo a partir de bloques declarativos."""
    err_html, stacks = _errors(errors)
    sections = _daily(daily) + ''.join(_table(t) for t in (tables or [])) + err_html

    islands = ''
    if daily:
        islands += ('<script type="application/json" id="dailySeries">'
                    + json.dumps(daily['series']) + '</script>')
        islands += ('<script type="application/json" id="dailyMetrics">'
                    + json.dumps(daily['metrics']) + '</script>')
    islands += '<script type="application/json" id="stackData">' + json.dumps(stacks) + '</script>'

    return (
        '<!DOCTYPE html>\n<html lang="' + lang + '">\n<head>\n'
        '<meta charset="UTF-8">\n'
        f'<meta http-equiv="refresh" content="{refresh_seconds}">\n'
        f'<title>{esc(title)}</title>\n'
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>\n'
        '<style>' + CSS + '</style>\n</head>\n<body>\n'
        '<header><div><h1>' + esc(title) + '</h1>'
        + (f'<small>{subtitle}</small>' if subtitle else '')
        + '</div><div><select class="tz-select" id="tzSelect">' + TZ_OPTIONS + '</select></div></header>\n'
        '<div class="container">\n'
        + _cards(cards) + sections +
        '\n</div>\n'
        + islands +
        '<script>' + JS + '</script>\n</body>\n</html>'
    )

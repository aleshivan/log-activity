"""Primitivas compartidas entre plugins (errores comunes, logins, helpers).

Cada plugin puede reusar estas reglas/extractores y añadir las suyas.
"""

import re

LOGIN_OK   = re.compile(r"login success for user '(.+?)'")
LOGIN_FAIL = re.compile(r'login failed', re.IGNORECASE)


def categorize_error(event, rules):
    """Aplica reglas ordenadas; cada regla es (category, label, fg, bg, border, match_fn).

    match_fn(event, full) -> bool. La primera que casa gana; si ninguna, OTHER.
    Devuelve un dict listo para render: category/label/color/summary/detail/full.
    """
    full = event['msg'] + '\n' + '\n'.join(event.get('extra', []))
    for category, label, _fg, _bg, _border, match in rules:
        if match(event, full):
            return {
                'category': category, 'label': label,
                'color': event['level'].lower(),
                'summary': event['msg'][:160],
                'detail': event['logger'],
                'full': full,
            }
    return {
        'category': 'OTHER', 'label': 'Otro', 'color': event['level'].lower(),
        'summary': event['msg'][:160], 'detail': event['logger'], 'full': full,
    }


# Regla común: tráfico de sondeo / peticiones sin autorización (igual que precision).
UNAUTH_PROBE_RULE = (
    'UNAUTH_PROBE', 'Sondeo no autorizado', '#9a3412', '#ffedd5', '#fb923c',
    lambda e, full: e['logger'].endswith('FallbackController')
    or 'Missing or invalid Authorization header' in full,
)


def category_meta(rules):
    """Construye el dict {category: (label, fg, bg, border)} para el render de errores."""
    meta = {cat: (label, fg, bg, border) for cat, label, fg, bg, border, _ in rules}
    meta.setdefault('OTHER', ('Otro', '#374151', '#f3f4f6', '#6b7280'))
    return meta

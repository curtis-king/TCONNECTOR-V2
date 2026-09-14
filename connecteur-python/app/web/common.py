"""Helpers partagés entre tous les blueprints web.

Déplacé depuis app/web/parking/common.py (étape A7). La liaison tardive vers
app.web.dashboard a disparu : les helpers d'identité/CSRF viennent directement
de app.web.auth.security (aucun import circulaire). Le CSS et le JS partagés
sont servis via url_for('static', ...) dans les templates (étape A7 :
externalisation, plus d'injection inline).
"""

import html

from flask import render_template, request, session, url_for

from app.web.auth import user_auth
from app.web.auth.security import _current_identity, _get_csrf_token


def _esc(val):
    if val is None:
        return ""
    return html.escape(str(val))


def _static_tags():
    """Balises de chargement du CSS et du JS partagés (fichiers statiques)."""
    return ('<link rel="stylesheet" href="{css}">'
            '<script src="{js}"></script>').format(
        css=url_for("static", filename="css/app.css"),
        js=url_for("static", filename="js/app.js"),
    )



SIDEBAR_ICONS = {
    "dashboard": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/></svg>',
    "invoice": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
    "card": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/></svg>',
    "cart": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg>',
    "users": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    "clock": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
    "user": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
    "hourglass": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 22h14"/><path d="M5 2h14"/><path d="M17 22v-4.172a2 2 0 0 0-.586-1.414L12 12l-4.414 4.414A2 2 0 0 0 7 17.828V22"/><path d="M7 2v4.172a2 2 0 0 0 .586 1.414L12 12l4.414-4.414A2 2 0 0 0 17 6.172V2"/></svg>',
    "check": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
    "settings": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    "logout": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
}


SIDEBAR_ITEMS = [
    ("/", "dashboard", "Dashboard"),
    ("/invoices", "invoice", "Factures Sage"),
    ("/billing", "card", "Facturation"),
    ("/pos", "cart", "Vente POS"),
    ("/clients", "users", "Clients"),
    ("/sales", "clock", "Historique"),
    ("/vendeurs", "user", "Vendeurs"),
    ("/pending", "hourglass", "En attente"),
    ("/certified", "check", "Certifiees"),
    ("/utilisateurs", "settings", "Utilisateurs"),
    ("/config", "settings", "Config"),
]


SIDEBAR_PERM = {
    "/": "dashboard.voir",
    "/invoices": "factures.voir",
    "/billing": "factures.voir",
    "/pos": "pos.vente",
    "/clients": "clients.gerer",
    "/sales": "factures.voir",
    "/vendeurs": "vendeurs.gerer",
    "/pending": "factures.voir",
    "/certified": "factures.voir",
    "/utilisateurs": "utilisateurs.gerer",
    "/config": "config.gerer",
}


def _sidebar(path="../.."):
    def _item(url, icon, label):
        if url == "/":
            active = "active" if path == "/" else ""
        else:
            active = "active" if path.startswith(url) else ""
        return '<li><a href="{url}" class="{active}"><span class="icon">{icon}</span><span class="label">{label}</span></a></li>'.format(
            url=url, active=active, icon=SIDEBAR_ICONS[icon], label=label
        )

    role = session.get("user_role", "")
    items = SIDEBAR_ITEMS
    if role:
        items = [it for it in SIDEBAR_ITEMS
                 if role == "admin" or user_auth.has_perm(role, SIDEBAR_PERM.get(it[0], "dashboard.voir"))]

    groups = [
        ("Principal", items[:4]),
        ("Gestion", items[4:]),
    ]
    nav_parts = []
    for gi, (group_name, group_items) in enumerate(groups):
        nav_parts.append('<li class="nav-label">' + group_name + "</li>")
        nav_parts.extend(_item(*item) for item in group_items)
    nav = "".join(nav_parts)
    nav += ('<li><a href="/logout" class="logout-link"><span class="icon">{icon}</span><span class="label">Deconnexion</span></a></li>').format(
        icon=SIDEBAR_ICONS["logout"]
    )

    return """<div class="sidebar">
  <div class="sidebar-top">
    <a href="/" class="sidebar-logo">
      <div class="icon">TC</div>
      <span>T-CONNECTOR</span>
    </a>
    <button class="sidebar-toggle no-print" onclick="toggleSidebar()" aria-label="Replier le menu">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
    </button>
  </div>
  <ul class="sidebar-nav">""" + nav + """</ul>
  <div class="sidebar-foot">
    <div class="net-indicator">
      <span class="net-dot unknown" id="net-dot"></span>
      <span id="net-label" style="font-size:11px;font-weight:600">...</span>
    </div>
  </div>
</div>
"""


ALERT_ZONE = """<div class="alert-zone" id="alert-zone" aria-live="polite"></div>"""


FOOTER = """</div>
""" + ALERT_ZONE + """
</body></html>"""


def _current_page():
    p = request.path
    if p == "/":
        return "Dashboard", "Tableau de bord"
    for url, _ic, label in SIDEBAR_ITEMS:
        if p.startswith(url) and url != "/":
            return url.strip("/") or "page", label
    return "", p.strip("/")


def _base_context():
    page_key, page_label = _current_page()
    identity = _current_identity() or {}
    email = identity.get("email", "")
    initial = (email[:1].upper() if email else "U")
    role_label = user_auth.ROLE_LABELS.get(identity.get("role", ""), identity.get("role", ""))
    topbar = ('<div class="topbar"><div class="topbar-title">{title}</div>'
              '<div class="topbar-actions">'
              '{sync}'
              '<span class="user-chip"><span class="ava">{initial}</span>'
              '<span>{email}<span style="color:#64748b;font-weight:500"> &middot; {role_label}</span></span></span>'
              '<a class="btn btn-sm btn-ghost no-print" href="/compte/mot-de-passe">Mot de passe</a>'
              '<a class="btn btn-sm btn-ghost no-print" href="/logout">Deconnexion</a>'
              '</div></div>').format(
        title=_esc(page_label),
        sync=('<button class="btn btn-sm btn-primary no-print" onclick="syncNow()">Synchroniser</button>' if page_key != "config" else ""),
        initial=_esc(initial), email=_esc(email), role_label=_esc(role_label)
    )
    return {
        "page_label": _esc(page_label),
        "static_tags": _static_tags(),
        "sidebar_html": _sidebar(request.path),
        "topbar_html": topbar,
        "csrf_token": _esc(_get_csrf_token()),
        "footer_html": FOOTER,
    }


def _page(body):
    return render_template("base.html", body=body, **_base_context())



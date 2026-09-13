"""Parking common — helpers partagés entre tous les domaines.

Constantes UI (CSS, sidebar, ALERT_ZONE, FOOTER) et helpers de rendu
(_esc, _current_page, _base_context, _page) déplacés mécaniquement
depuis app/web/dashboard.py (étape A3). Corps strictement inchangés.
"""

import html

from flask import render_template, request, session

from app.web.auth import user_auth
from app.web.static_content import read_static


def __getattr__(name):
    """Filet de sécurité : délégation vers app.web.dashboard pour tout nom
    non résolu (uniquement effectif sur accès attribut du module). Les noms
    réellement utilisés par les handlers sont liés explicitement en bas de
    ce module (voir section « Liaison dashboard »)."""
    from app.web import dashboard as _dashboard
    return getattr(_dashboard, name)


def _esc(val):
    if val is None:
        return ""
    return html.escape(str(val))


CSS = read_static("css/app.css")


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


ALERT_ZONE = """<div class="alert-zone" id="alert-zone" aria-live="polite"></div>
<script>
var _alertTimers = {};
function showToast(msg, type) {
  type = type || "ok";
  var scanFeed = (type === "err") && (msg.indexOf("Code-barres inconnu") === 0 || msg.indexOf("Erreur scan") === 0);
  if (window.NATIVE_ALERTS && !scanFeed) { alert(msg); return; }
  if (window.POS_DIALOG_ALERTS && type === "err" && !scanFeed) {
    showDialog(msg, "err");
    return;
  }
  var zone = document.getElementById("alert-zone");
  if (!zone) return;
  var key = type + "::" + msg;
  var prev = null;
  for (var i = 0; i < zone.children.length; i++) {
    if (zone.children[i].getAttribute("data-key") === key) { prev = zone.children[i]; break; }
  }
  if (prev) { clearTimeout(_alertTimers[key]); zone.removeChild(prev); }
  var card = document.createElement("div");
  card.className = "alert-card alert-" + type;
  card.setAttribute("data-key", key);
  var icon = "\u2714";
  if (type === "info") icon = "i";
  else if (type === "warn") icon = "!";
  else if (type === "err") icon = "\u2716";
  card.innerHTML = '<span class="alert-icon">' + icon + '</span><span class="alert-msg"></span><button class="alert-x" role="button" aria-label="Fermer">\u00d7</button>';
  card.querySelector(".alert-msg").textContent = msg;
  var close = card.querySelector(".alert-x");
  close.onclick = function() { dismissAlert(card, key); };
  zone.appendChild(card);
  var delay = type === "warn" ? 7000 : (type === "err" ? 12000 : 3800);
  _alertTimers[key] = setTimeout(function() { dismissAlert(card, key); }, delay);
  while (zone.children.length > 4) {
    var old = zone.children[0];
    dismissAlert(old, old.getAttribute("data-key"));
  }
}
function dismissAlert(card, key) {
  if (!card) return;
  if (_alertTimers[key]) clearTimeout(_alertTimers[key]);
  delete _alertTimers[key];
  card.classList.add("alert-out");
  setTimeout(function() { if (card.parentNode) card.parentNode.removeChild(card); }, 260);
}
function showDialog(msg, type, title) {
  type = type || "info";
  var root = document.getElementById("dialog-root");
  if (!root) { root = document.createElement("div"); root.id = "dialog-root"; document.body.appendChild(root); }
  var icons = {ok: "\u2714", info: "i", warn: "!", err: "\u2716"};
  var t = title || (type === "err" ? "Erreur" : type === "warn" ? "Attention" : type === "ok" ? "Succes" : "Information");
  root.innerHTML = '<div class="alert-overlay" id="dlg-overlay">'
    + '<div class="alert-dialog alert-' + type + '" role="dialog" aria-modal="true">'
    + '<div class="alert-dialog-head"><span class="alert-dialog-icon">' + (icons[type] || "i") + '</span><span class="alert-dialog-title"></span></div>'
    + '<div class="alert-dialog-msg"></div>'
    + '<div class="alert-dialog-actions"><button class="btn" id="dlg-ok">OK</button></div>'
    + '</div></div>';
  root.querySelector(".alert-dialog-title").textContent = t;
  root.querySelector(".alert-dialog-msg").textContent = msg;
  var overlay = root.querySelector("#dlg-overlay");
  var dialog = root.querySelector(".alert-dialog");
  function close() {
    if (document.getElementById("dlg-ok")) document.removeEventListener("keydown", onKey);
    overlay.classList.add("alert-out");
    dialog.classList.add("alert-out");
    setTimeout(function() { if (root.parentNode) root.parentNode.removeChild(root); }, 200);
  }
  function onKey(e) { if (e.key === "Escape") close(); }
  document.addEventListener("keydown", onKey);
  root.querySelector("#dlg-ok").onclick = close;
  overlay.addEventListener("click", function(e) { if (e.target === overlay) close(); });
  var ok = root.querySelector("#dlg-ok");
  if (ok) ok.focus();
}
function checkConnectivity() {
  fetch("/api/connectivity").then(function(r){return r.json()}).then(function(d){
    var dot = document.getElementById("net-dot");
    var lbl = document.getElementById("net-label");
    if (d.online) {
      dot.className = "net-dot on";
      lbl.textContent = "EN LIGNE";
    } else {
      dot.className = "net-dot off";
      lbl.textContent = "HORS LIGNE";
    }
  }).catch(function(){
    document.getElementById("net-dot").className = "net-dot off";
    document.getElementById("net-label").textContent = "HORS LIGNE";
  });
}
checkConnectivity();
setInterval(checkConnectivity, 15000);
</script>"""


FOOTER = """</div>
""" + ALERT_ZONE + """
<script>
function api(m,u,b){function call(t){var h={"Content-Type":"application/json"};if(t){window.CSRF_TOKEN=t;h["X-CSRF-Token"]=t}return fetch(u,{method:m,headers:h,body:b?JSON.stringify(b):undefined}).then(function(r){return r.json().then(function(d){if((r.status===400||r.status===403)&&d&&d.error&&d.error.indexOf("CSRF")>=0&&!t){return fetch("/api/csrf").then(function(r){return r.json()}).then(function(x){return call(x.token)})}return d})})}if(window.CSRF_TOKEN)return call(window.CSRF_TOKEN);return fetch("/api/csrf").then(function(r){return r.json()}).then(function(d){return call(d.token)})}
function syncNow(){showToast("Sync en cours...","info");api("POST","/api/sync").then(function(d){showToast(d.message||"OK","ok");setTimeout(function(){location.reload()},2000)})}
function switchTab(id){document.querySelectorAll(".tab-btn").forEach(function(b){b.classList.remove("active")});document.querySelectorAll(".tab-content").forEach(function(c){c.classList.remove("active")});document.getElementById("tab-btn-"+id).classList.add("active");document.getElementById("tab-"+id).classList.add("active")}
function saveForm(formId, url, next){var f=document.getElementById(formId);var d=new FormData(f);var obj={};d.forEach(function(v,k){obj[k]=v});api("POST",url,obj).then(function(r){if(r.ok){showToast("Sauvegarde OK","ok");if(next)next()}else{showToast("Erreur: "+(r.error||"inconnue"),"err")}}).catch(function(e){showToast("Erreur reseau: "+e,"err")})}
function toggleSidebar(){var s=document.querySelector(".sidebar");if(!s)return;var c=s.classList.toggle("collapsed");try{localStorage.setItem("tconn_sidebar",c?"1":"0")}catch(e){}}
(function(){try{if(localStorage.getItem("tconn_sidebar")==="1"){var s=document.querySelector(".sidebar");if(s)s.classList.add("collapsed")}}catch(e){}})();
</script></body></html>"""


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
        "css": CSS,
        "sidebar_html": _sidebar(request.path),
        "topbar_html": topbar,
        "csrf_token": _esc(_get_csrf_token()),
        "footer_html": FOOTER,
    }


def _page(body):
    return render_template("base.html", body=body, **_base_context())


# ── Liaison dashboard ──
# Ces noms sont définis dans app/web/dashboard.py. On les lie ICI, en bas de
# module : quel que soit l'ordre d'import (dashboard d'abord ou parking
# d'abord), ils existent déjà dans le namespace de dashboard.py à ce stade —
# aucun import circulaire possible. Les corps des fonctions ci-dessus restent
# strictement inchangés (références globales résolues à l'exécution).
from app.web import dashboard as _dashboard
_current_identity = _dashboard._current_identity
_get_csrf_token = _dashboard._get_csrf_token

del _dashboard

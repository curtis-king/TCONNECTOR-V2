import html
import logging
import functools
import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import timedelta
from flask import (
    Flask, render_template, render_template_string, request, jsonify,
    session, redirect, url_for
)
from app.config.manager import get_config, save_config
from app.web.auth import user_auth
from app.sync.engine import (
    get_cache, get_metrics, sync_all, certify_single, sync_sfec_invoices,
    apply_config, get_retry_queue
)
from app.integration.sage.database import (
    ping_database, fetch_contacts, fetch_tax_rates,
    fetch_ledger_accounts, fetch_certified_invoices, list_all_tables,
    mark_to_monitor
)
from app.integration.sfec.endpoints import check_health, preview_sfec_payload, validate_sfec_payload, certify_sqlite_invoice
from app.integration.sfec.client import SfecClient
from app.sync.connectivity import get_status, check_now
from app.storage import db as sqlite_db
from app.domain import invoices as invoice_engine
from app.domain import pos as pos_engine
from app.integration.sage import writer as sage_writer
from app.sync import bidirectional as sync_bidirectional
from app.domain import pdf as pdf_generator
from app.web.static_content import read_static

logger = logging.getLogger("t-connector.dashboard")

app = Flask(__name__)


def _esc(val):
    if val is None:
        return ""
    return html.escape(str(val))


def _get_secret_key():
    cfg = get_config().get("dashboard", {})
    key = cfg.get("session_secret", "")
    if not key:
        machine_id = os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "default"))
        key = hashlib.sha256("t-connector-{}".format(machine_id).encode()).hexdigest()[:32]
    return key


app.secret_key = _get_secret_key()


def _auth_enabled():
    return get_config().get("dashboard", {}).get("auth_enabled", True)


_applied = False


def _apply_session_security():
    global _applied
    if _applied:
        return
    _applied = True
    from app.web.auth.user_auth import get_auth_config
    acfg = get_auth_config()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=acfg.get("https_only", False),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=acfg.get("session_max_age_hours", 24)),
        SESSION_REFRESH_EACH_REQUEST=True,
    )


_apply_session_security()


def _get_csrf_token():
    if not session.get("_csrf"):
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def _csrf_valid():
    token = (request.headers.get("X-CSRF-Token") or request.form.get("_csrf")
             or (request.get_json(silent=True) or {}).get("_csrf") or "")
    stored = session.get("_csrf") or ""
    return bool(stored) and hmac.compare_digest(stored, token)


def _current_identity():
    if not session.get("user_id"):
        return None
    return {
        "user_id": session.get("user_id"),
        "email": session.get("user_email", ""),
        "role": session.get("user_role", ""),
        "nom": session.get("user_nom", ""),
        "prenom": session.get("user_prenom", ""),
    }


def _is_sage_connected_user():
    """Compte dont le mot de passe est gere par Sage (provider sage)."""
    return get_config().get("auth", {}).get("provider", "local") == "sage"


def _login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not _auth_enabled():
            return f(*args, **kwargs)
        identity = _current_identity()
        if not identity:
            return redirect(url_for("login_page"))
        try:
            live = user_auth.get_user(identity["user_id"])
        except Exception:
            live = None
        if not live or not live.get("est_actif"):
            session.clear()
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


# ── Controle des droits : chemin -> permission ──

_ACCESS_RULES = [
    (("config",), "config.gerer"),
    (("api", "config"), "config.gerer"),
    (("utilisateurs",), "utilisateurs.gerer"),
    (("api", "utilisateurs"), "utilisateurs.gerer"),
    (("api", "audit"), "utilisateurs.gerer"),
    (("api", "auth"), "utilisateurs.gerer"),
    (("pos",), "pos.vente"),
    (("api", "pos"), "pos.vente"),
    (("api", "products"), "pos.vente"),
    (("api", "articles"), "pos.vente"),
    (("api", "sfec"), "sfec.certifier"),
    (("api", "sync"), "sage.sync"),
    (("invoices",), "factures.voir"),
    (("billing",), "factures.voir"),
    (("sales",), "factures.voir"),
    (("pending",), "factures.voir"),
    (("certified",), "factures.voir"),
    (("api", "certified"), "factures.voir"),
    (("api", "retry-queue"), "factures.voir"),
    (("api", "billing"), "factures.voir"),
    (("clients",), "clients.gerer"),
    (("api", "clients"), "clients.voir"),
    (("api", "contacts"), "clients.voir"),
    (("vendeurs",), "vendeurs.gerer"),
    (("api", "vendeurs"), "vendeurs.voir"),
    (("api", "invoices", "stats"), "factures.voir"),
    (("api", "invoices"), "factures.voir"),
]

_MUTATE_PERMS = {
    ("/api/clients", "POST"): "clients.gerer",
    ("/api/contacts", "POST"): "clients.gerer",
    ("/api/vendeurs", "POST"): "vendeurs.gerer",
    ("/api/products", "POST"): "pos.vente",
}


def _required_permission(path, method):
    parts = [p for p in path.strip("/").lower().split("/")][:3]
    role = session.get("user_role") or ""
    if role == "admin":
        return None, None
    for key, perm in _MUTATE_PERMS.items():
        if key == (path, method):
            return perm, None
    if path.endswith("/push-sage") and method == "POST":
        return "sage.pousser", None
    if path == "/api/invoices" and method == "POST":
        return "factures.creer", None
    if path.startswith("/api/invoices/") and method in ("PUT", "DELETE"):
        return "factures.creer", None
    for prefixes, perm in _ACCESS_RULES:
        if len(prefixes) <= len(parts) and all(parts[i] == prefixes[i] for i in range(len(prefixes))):
            return perm, None
    return None, None


def _deny(perm, label):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Acces refuse (droits insuffisants)", "permission": perm}), 403
    return render_template("auth/deny.html",
                           label=_esc(label or ""), email=_esc(session.get("user_email", ""))), 403


@app.before_request
def _auth_before_request():
    if not _auth_enabled():
        return None
    _apply_session_security()
    path = request.path

    if session.get("user_id") and not session.get("_login_checked"):
        live = None
        try:
            live = user_auth.get_user(session.get("user_id"))
        except Exception:
            pass
        if not live or not live.get("est_actif"):
            session.clear()
            return redirect(url_for("login_page"))
        session["user_email"] = live["email"]
        session["user_role"] = live["role"]
        session["user_nom"] = live["nom"]
        session["user_prenom"] = live["prenom"]
        session["_login_checked"] = True

    if session.get("user_id") and not session.get("authenticated"):
        session["authenticated"] = True

    acfg = user_auth.get_auth_config()
    idle_min = acfg.get("session_idle_minutes", 0)
    if idle_min > 0 and session.get("user_id") and not path.startswith("/api/connectivity"):
        import time as _time
        last = session.get("_last_activity")
        now = _time.time()
        if last is None:
            session["_last_activity"] = now
        elif now - float(last) > idle_min * 60:
            user_auth.audit("session.expiree", "Expiration pour inactivite",
                            email=session.get("user_email", ""))
            session.clear()
            if path.startswith("/api/"):
                return jsonify({"error": "Session expiree"}), 401
            return redirect(url_for("login_page"))
        else:
            session["_last_activity"] = now

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if session.get("user_id") and not _csrf_valid():
            user_auth.audit("csrf.rejete", "Requete {} sans jeton CSRF valide".format(request.method),
                            email=session.get("user_email", ""))
            if path.startswith("/api/"):
                return jsonify({"error": "Jeton CSRF invalide"}), 400
            return render_template(
                "auth/deny.html",
                label=_esc("Jeton de securite invalide"),
                email=_esc(session.get("user_email", ""))
            ), 400

    if path in ("/login", "/logout") or path.startswith("/static/"):
        return None
    if not session.get("user_id"):
        if path.startswith("/api/"):
            return jsonify({"error": "Non authentifie"}), 401
        return redirect(url_for("login_page"))
    perm, _m = _required_permission(path, request.method)
    if perm and not user_auth.has_perm(session.get("user_role", ""), perm):
        return _deny(perm, user_auth.PERMISSIONS.get(perm, perm))
    return None


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'")
    if session.get("user_id"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


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


def _login_csrf_field():
    return ('<input type="hidden" name="_csrf" value="{}">').format(_esc(_get_csrf_token()))


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if not _auth_enabled():
        if request.method == "POST":
            return redirect("/")
        return redirect("/")
    if request.method == "GET":
        if session.get("user_id"):
            return redirect("/")
        return render_template("auth/login.html", error_html="", csrf_field=_login_csrf_field())

    _apply_session_security()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    if not session.get("_csrf") or not _csrf_valid():
        return render_template(
            "auth/login.html",
            error_html='<div class="error">Session expirée - reessayez</div>',
            csrf_field=_login_csrf_field()
        ), 400
    user, err = user_auth.authenticate(email, password, ip=request.remote_addr or "")
    if not user:
        return render_template(
            "auth/login.html",
            error_html='<div class="error">{msg}</div>'.format(msg=_esc(err or "Erreur")),
            csrf_field=_login_csrf_field()
        ), 401

    session.clear()
    session["user_id"] = user["id"]
    session["user_email"] = user["email"]
    session["user_role"] = user["role"]
    session["user_nom"] = user.get("nom", "")
    session["user_prenom"] = user.get("prenom", "")
    session["authenticated"] = True
    session["_login_checked"] = True
    import time as _time
    session["_last_activity"] = _time.time()
    session.permanent = True
    app.permanent_session_lifetime = timedelta(hours=user_auth.get_auth_config().get("session_max_age_hours", 24))
    session["_csrf"] = secrets.token_urlsafe(32)

    email_clean = user["email"]
    session["vendeur_id"] = None
    try:
        with sqlite_db.get_cursor() as cur:
            cur.execute("SELECT id FROM vendeurs WHERE email = ? AND est_actif = 1", (email_clean,))
            row = cur.fetchone()
            session["vendeur_id"] = row["id"] if row else None
    except Exception:
        session["vendeur_id"] = None
    return redirect("/")


@app.route("/logout")
def logout():
    user_auth.audit("deconnexion", "Deconnexion", email=session.get("user_email", ""),
                    ip=request.remote_addr or "")
    session.clear()
    return redirect("/login")


@app.route("/")
@_login_required
def index():
    m = get_metrics()
    sfec = check_health()
    conn = get_status()
    retry_q = get_retry_queue()

    cache = get_cache()
    val_count = sum(1 for inv in cache.get("sales_invoices", []) if inv.get("valide", 0) == 1)

    db_ok = "badge-ok" if m.get("db_connected") else "badge-err"
    db_txt = "Connectee" if m.get("db_connected") else "Deconnectee"
    sfec_ok = "badge-ok" if sfec.get("connected") else "badge-err"
    sfec_txt = "Connectee" if sfec.get("connected") else "Deconnectee"
    net_cls = "badge-ok" if conn.get("online") else "badge-err"
    net_txt = "EN LIGNE" if conn.get("online") else "HORS LIGNE"

    retry_html = ""
    if retry_q:
        retry_html = '<tr><td>File d\'attente SFEC</td><td><span class="queue-badge">{} factures en attente</span></td></tr>'.format(len(retry_q))

    return _page(render_template("dashboard/index.html",
        sales=m.get("sales_invoices", 0), purchases=m.get("purchase_invoices", 0),
        val_count=val_count,
        contacts=m.get("contacts", 0), tax=m.get("tax_rates", 0),
        syncs=m.get("sync_count", 0), certs=m.get("certified_count", 0),
        db_ok=db_ok, db_txt=db_txt, last_sync=_esc(m.get("last_sync_at") or "Jamais"),
        error=_esc(m.get("last_error") or "Aucune"),
        sfec_ok=sfec_ok, sfec_txt=sfec_txt, sfec_url=_esc(sfec.get("url", "")),
        net_cls=net_cls, net_txt=net_txt, retry_html=retry_html
    ))


@app.route("/invoices")
@_login_required
def invoices_page():
    inv_type = request.args.get("type", "sale")
    cache = get_cache()
    invoices = cache.get("purchase_invoices" if inv_type == "purchase" else "sales_invoices", [])
    rows = ""
    for inv in invoices[:200]:
        s = inv.get("statut", "")
        sb = "badge-ok" if s == "COMPTABILISE" else "badge-warn" if s in ("CONFIRME", "A COMPTABILISER") else ""
        ss = inv.get("sfec_statut", "")
        sfb = "badge-ok" if ss in ("CERTIFIE", "DEJA_CERTIFIE") else "badge-warn" if ss == "EN_COURS" else "badge-err" if ss == "ERREUR" else "badge-info" if ss == "A_SURVEILLER" else ""
        sftxt = "Certifie" if ss == "CERTIFIE" else "Deja certifie" if ss == "DEJA_CERTIFIE" else "En cours" if ss == "EN_COURS" else "Erreur" if ss == "ERREUR" else "A surveiller" if ss == "A_SURVEILLER" else "A certifier" if inv_type == "sale" else "-"
        cert_num = inv.get("sfec_num_certif", "") or ""
        val = inv.get("valide", 0)
        val_cls = "badge-ok" if val == 1 else "badge-err"
        val_txt = "Valide" if val == 1 else "Non valide"
        rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{:,.2f}</td><td><span class='badge {}'>{}</span></td><td><span class='badge {}'>{}</span></td><td><span class='badge {}'>{}</span></td><td>{}</td></tr>".format(
            _esc(inv.get("numero", "")), _esc(inv.get("date_facture", "")), _esc(inv.get("nom_tiers", "")),
            inv.get("montant_ttc", 0), sb, _esc(s), val_cls, val_txt, sfb, _esc(sftxt), _esc(cert_num or "-")
        )
    sale_cls = "btn-primary" if inv_type == "sale" else ""
    purchase_cls = "btn-primary" if inv_type == "purchase" else ""
    empty = '<tr><td colspan="8" style="text-align:center;color:#64748b">Aucune facture</td></tr>' if not rows else ""
    return _page(render_template("dashboard/invoices.html",
        inv_type=inv_type, sale_cls=sale_cls, purchase_cls=purchase_cls,
        rows=rows, empty=empty
    ))


@app.route("/pending")
@_login_required
def pending_page():
    cache = get_cache()
    sfec_cfg = get_config().get("sfec", {})
    sfec_ids = set()
    try:
        client = SfecClient()
        sfec_result = client.list_invoices(page=1, page_size=500)
        for inv in sfec_result.get("invoices", []):
            seller_inv = inv.get("seller_invoice_number", "") or inv.get("invoice_number", "")
            if seller_inv:
                sfec_ids.add(seller_inv)
    except Exception:
        pass
    uncertified = [
        inv for inv in cache.get("sales_invoices", [])
        if inv.get("numero", "") not in sfec_ids
        and inv.get("valide", 0) == 1
        and inv.get("sfec_statut", "") not in ("EN_COURS", "ERREUR")
    ]
    uncertified.sort(key=lambda x: x.get("date_facture", ""), reverse=True)
    val_count = sum(1 for inv in cache.get("sales_invoices", []) if inv.get("valide", 0) == 1)
    rows = ""
    for inv in uncertified[:200]:
        rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{:,.2f}</td><td><button class='btn btn-sm btn-success' onclick=\"certifySingle('{}')\">Certifier</button> <button class='btn btn-sm' style='background:#4a1d96;color:#fff' onclick=\"toMonitor('{}')\">Surveiller</button></td></tr>".format(
            _esc(inv.get("numero", "")), _esc(inv.get("date_facture", "")), _esc(inv.get("nom_tiers", "")),
            inv.get("montant_ttc", 0), inv.get("id", ""), inv.get("id", "")
        )
    sfec_warn = ""
    if not sfec_cfg.get("enabled"):
        sfec_warn = '<p class="badge badge-err" style="margin-bottom:12px">SFEC desactivee</p>'
    empty = '<tr><td colspan="5" style="text-align:center;color:#64748b">Toutes certifiees !</td></tr>' if not rows else ""
    return _page(render_template("dashboard/pending.html",
        count=len(uncertified),
        sfec_warn=sfec_warn, val_count=val_count, rows=rows, empty=empty
    ))


@app.route("/certified")
@_login_required
def certified_page():
    cache = get_cache()
    last_sfec = cache.get("last_sfec_sync_at", "")
    return _page(render_template("dashboard/certified.html",
                                 last_sfec=(last_sfec or "")[:19].replace("T", " ") or "jamais"))


@app.route("/certified/<invoice_id>/print")
@_login_required
def print_certified(invoice_id):
    inv = None
    try:
        client = SfecClient()
        inv = client.get_invoice(invoice_id)
    except Exception as e:
        logger.warning("Impossible de recuperer la facture SFEC %s: %s", invoice_id, e)

    if not inv:
        return "<h1>Facture non trouvee</h1><p><a href='/certified'>Retour</a></p>", 404

    company = get_config().get("company", {})
    qr_code = inv.get("qr_code") or inv.get("certification_qr_code") or ""
    is_image = qr_code.startswith("data:")
    if qr_code:
        qr_display = '<img src="{}" style="max-width:200px;max-height:200px">'.format(qr_code) if is_image else '<pre style="font-size:10px;word-break:break-all">{}</pre>'.format(qr_code)
    else:
        qr_display = '<p style="color:#999">QR Code non disponible</p>'

    items = inv.get("items_json") or []
    item_rows = ""
    for item in items:
        item_rows += "<tr><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.0f}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.0f}</td></tr>".format(
            _esc(item.get("designation", "")),
            item.get("quantity", 0),
            item.get("unit_price", 0),
            _esc(item.get("tax_rate", "0")),
            item.get("net_amount", 0),
        )

    return render_template("dashboard/print.html",
        invoice_number=_esc(inv.get("invoice_number", "")),
        invoice_date=_esc((inv.get("invoice_date") or "")[:10]),
        currency=_esc(inv.get("currency", "XAF")),
        seller_name=_esc(inv.get("seller_name", company.get("name", ""))),
        seller_addr=_esc(inv.get("seller_address", company.get("address", ""))),
        seller_niu=_esc(inv.get("seller_niu", company.get("tax_number", ""))),
        seller_rccm=_esc(inv.get("seller_rccm", "")),
        buyer_name=_esc(inv.get("buyer_name", "")),
        buyer_niu=_esc(inv.get("buyer_niu", "")),
        buyer_addr=_esc(inv.get("buyer_address", "")),
        buyer_phone=_esc(inv.get("buyer_phone", "")),
        payment_method=_esc(inv.get("payment_method", "")),
        amount_due=_esc(inv.get("amount_due", "0")),
        total_ht=_esc(inv.get("total_ht", "0")),
        total_tax18=_esc(inv.get("total_tax18", "0")),
        total_tax5=_esc(inv.get("total_tax5", "0")),
        total_ttc=_esc(inv.get("total_ttc", "0")),
        item_rows=item_rows,
        cert_status=_esc(inv.get("certification_status", "")),
        short_sig=_esc(inv.get("certification_short_signature", "")),
        signature=_esc(inv.get("certification_signature", "")),
        cert_date=_esc((inv.get("certification_date") or "")[:19].replace("T", " ")),
        qr_display=qr_display,
    )


# ══════════════════════════════════════════════════════════════
# CONFIG PAGE - 7 ONGLETS
# ══════════════════════════════════════════════════════════════

@app.route("/config")
@_login_required
def config_page():
    cfg = get_config()
    db = cfg.get("db", {})
    sfec = cfg.get("sfec", {})
    company = cfg.get("company", {})
    dash = cfg.get("dashboard", {})
    queue = cfg.get("queue", {})
    validation = cfg.get("validation", {})
    notifications = cfg.get("notifications", {})
    rate_limit = cfg.get("rate_limit", {})
    log_level = cfg.get("log_level", "info")

    def _s(val, target):
        return "selected" if val == target else ""

    def _ck(val):
        return "checked" if val else ""

    # ── TAB 5: VALIDATION & LIMITES ──
    notif_events = notifications.get("events", [])
    body = render_template("config/index.html",
        server=_esc(db.get("server", "")),
        port=_esc(db.get("port", 1433)),
        database=_esc(db.get("database", "")),
        user=_esc(db.get("user", "")),
        password=_esc(db.get("password", "")),
        tc_f=_s(db.get("trusted_connection"), False),
        tc_t=_s(db.get("trusted_connection"), True),
        enc_f=_s(db.get("encrypt"), False),
        enc_t=_s(db.get("encrypt"), True),
        tsc_t=_s(db.get("trust_server_certificate"), True),
        tsc_f=_s(db.get("trust_server_certificate"), False),
        pool_max=db.get("pool_max", 10),
        pool_min=db.get("pool_min", 2),
        pool_idle_timeout_ms=db.get("pool_idle_timeout_ms", 30000),
        connection_timeout_ms=db.get("connection_timeout_ms", 15000),
        request_timeout_ms=db.get("request_timeout_ms", 30000),
        api_key=_esc(sfec.get("api_key", "")),
        sb_t=_s(sfec.get("use_sandbox"), True),
        sb_f=_s(sfec.get("use_sandbox"), False),
        en_t=_s(sfec.get("enabled"), True),
        en_f=_s(sfec.get("enabled"), False),
        cs_sa=_s(sfec.get("certify_status"), "SAISI"),
        cs_co=_s(sfec.get("certify_status"), "CONFIRME"),
        cs_ac=_s(sfec.get("certify_status"), "A COMPTABILISER"),
        cs_c=_s(sfec.get("certify_status"), "COMPTABILISE"),
        cs_l=_s(sfec.get("certify_status"), "LETTRÉ"),
        base_url=_esc(sfec.get("base_url", "")),
        sandbox_url=_esc(sfec.get("sandbox_url", "")),
        name=_esc(company.get("name", "")),
        tax_number=_esc(company.get("tax_number", "")),
        address=_esc(company.get("address", "")),
        city=_esc(company.get("city", "")),
        country=_esc(company.get("country", "")),
        phone=_esc(company.get("phone", "")),
        email=_esc(company.get("email", "")),
        cur_xaf=_s(company.get("currency"), "XAF"),
        cur_usd=_s(company.get("currency"), "USD"),
        bank_name=_esc(company.get("bank_name", "")),
        bank_account=_esc(company.get("bank_account", "")),
        bank_iban=_esc(company.get("bank_iban", "")),
        bank_swift=_esc(company.get("bank_swift", "")),
        invoice_prefix=_esc(company.get("invoice_prefix", "INV-")),
        invoice_next_number=_esc(company.get("invoice_next_number", 1)),
        invoice_notes=_esc(company.get("invoice_notes", "")),
        invoice_footer=_esc(company.get("invoice_footer", "")),
        tax_regime=_esc(company.get("tax_regime", "")),
        qr_t=_s(company.get("qrcode_enabled"), True),
        qr_f=_s(company.get("qrcode_enabled"), False),
        pe_t=_s(db.get("polling_enabled"), True),
        pe_f=_s(db.get("polling_enabled"), False),
        poll_ms=db.get("polling_interval_ms", 30000),
        max_concurrent=queue.get("max_concurrent", 5),
        max_retries=queue.get("max_retries", 4),
        retry_base_delay_ms=queue.get("retry_base_delay_ms", 2000),
        max_queue_size=queue.get("max_queue_size", 10000),
        job_timeout_ms=queue.get("job_timeout_ms", 60000),
        st_t=_s(validation.get("strict_mode"), True),
        st_f=_s(validation.get("strict_mode"), False),
        tolerance=validation.get("tolerance_amount", 0.01),
        daily_max=rate_limit.get("daily_max", 2500),
        per_minute_max=rate_limit.get("per_minute_max", 100),
        alert_threshold=rate_limit.get("alert_threshold_percent", 80),
        nt_t=_s(notifications.get("enabled"), True),
        nt_f=_s(notifications.get("enabled"), False),
        ev_synced=_ck("invoice:synced" in notif_events),
        ev_val=_ck("invoice:validation_error" in notif_events),
        ev_dlq=_ck("queue:dlq_added" in notif_events),
        ev_dbf=_ck("db:connection_failed" in notif_events),
        ev_dbe=_ck("db:polling_error" in notif_events),
        ll_d=_s(log_level, "debug"),
        ll_i=_s(log_level, "info"),
        ll_w=_s(log_level, "warning"),
        ll_e=_s(log_level, "error"),
        dash_port=_esc(dash.get("port", 3000)),
        auth_t=_s(dash.get("auth_enabled"), True),
        auth_f=_s(dash.get("auth_enabled"), False),
        login_email=_esc(dash.get("login_email", "")),
        login_password=_esc(dash.get("login_password", "")),
        session_secret=_esc(dash.get("session_secret", "")),
        session_max_hours=_esc(dash.get("session_max_age_hours", 24)),
        host=_esc(dash.get("host", "0.0.0.0")),
    )

    return _page(body)


# ══════════════════════════════════════════════════════════════
# API Routes
# ══════════════════════════════════════════════════════════════

def _bool(val):
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "on")
    return bool(val)


@app.route("/api/metrics")
@_login_required
def api_metrics():
    return jsonify(get_metrics())


@app.route("/api/connectivity")
@_login_required
def api_connectivity():
    return jsonify(get_status())


@app.route("/api/sync", methods=["POST"])
@_login_required
def api_sync():
    threading.Thread(target=sync_all, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync lancee"})


@app.route("/api/articles/sync", methods=["POST"])
@_login_required
def api_articles_sync():
    def _run():
        try:
            from app.sync import article_sync
            result = article_sync.sync_articles_from_sage()
            logger.info("Sync articles API: %s", result)
        except Exception as e:
            logger.error("Sync articles API: %s", e)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync des articles lancee"})


@app.route("/api/db/test")
@_login_required
def api_db_test():
    return jsonify(ping_database())


@app.route("/api/sfec/test")
@_login_required
def api_sfec_test():
    return jsonify(check_health())


@app.route("/api/sfec/certify", methods=["POST"])
@_login_required
def api_sfec_certify():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"success": False, "error": "invoice_id requis"}), 400

    if str(invoice_id).startswith("INV-"):
        try:
            inv_num = int(str(invoice_id).replace("INV-", ""))
        except ValueError:
            return jsonify({"success": False, "error": "Format ID invalide: {}".format(invoice_id)}), 400

        inv = invoice_engine.get_invoice(inv_num)
        if not inv:
            return jsonify({"success": False, "error": "Facture SQLite #{} non trouvee".format(inv_num)}), 404

        try:
            result = certify_sqlite_invoice(inv)
            user_auth.audit("facture.certifiee", "Certification SFEC {} #{}".format(
                result.get("sfec_statut", ""), result.get("numero", "")),
                email=(_current_identity() or {}).get("email", ""))
            return jsonify(result)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    return jsonify(certify_single(invoice_id))


@app.route("/api/sfec/to-monitor", methods=["POST"])
@_login_required
def api_sfec_to_monitor():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"success": False, "error": "invoice_id requis"}), 400
    try:
        mark_to_monitor(invoice_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sfec/debug", methods=["POST"])
@_login_required
def api_sfec_debug():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"error": "invoice_id requis"}), 400
    cache = get_cache()
    invoice = None
    for inv in cache.get("sales_invoices", []):
        if inv["id"] == invoice_id:
            invoice = inv
            break
    if not invoice:
        return jsonify({"error": "Facture non trouvee: {}".format(invoice_id)}), 404
    payload = preview_sfec_payload(invoice)
    return jsonify({"ok": True, "payload": payload, "invoice_data": {k: v for k, v in invoice.items() if k != "lignes"}})


@app.route("/api/sfec/validate", methods=["POST"])
@_login_required
def api_sfec_validate():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"error": "invoice_id requis"}), 400
    cache = get_cache()
    invoice = None
    for inv in cache.get("sales_invoices", []):
        if inv["id"] == invoice_id:
            invoice = inv
            break
    if not invoice:
        return jsonify({"error": "Facture non trouvee: {}".format(invoice_id)}), 404
    payload = preview_sfec_payload(invoice)
    errors = validate_sfec_payload(payload)
    return jsonify({"ok": len(errors) == 0, "errors": errors, "payload": payload})


@app.route("/api/sfec/sync", methods=["POST"])
@_login_required
def api_sfec_sync():
    def _do_sync():
        sync_sfec_invoices()
    t = threading.Thread(target=_do_sync, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Sync lancee en arriere-plan"})


@app.route("/api/sfec/certified-from-api")
@_login_required
def api_sfec_certified_from_api():
    try:
        client = SfecClient()
        result = client.list_invoices(page=1, page_size=100)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/contacts")
@_login_required
def api_contacts():
    return jsonify(fetch_contacts())


@app.route("/api/tax-rates")
@_login_required
def api_tax_rates():
    return jsonify(fetch_tax_rates())


@app.route("/api/ledger-accounts")
@_login_required
def api_ledger_accounts():
    return jsonify(fetch_ledger_accounts())


@app.route("/api/tables")
@_login_required
def api_tables():
    return jsonify(list_all_tables())


@app.route("/api/certified")
@_login_required
def api_certified():
    return jsonify(fetch_certified_invoices())


@app.route("/api/certified/list")
@_login_required
def api_certified_list():
    cache = get_cache()
    out = []
    for inv in cache.get("sfec_invoices", []):
        cd = inv.get("certification_date") or ""
        out.append({
            "id": inv.get("id", ""),
            "numero": inv.get("invoice_number", ""),
            "date": cd[:10] if cd else "",
            "statut": (inv.get("invoice_status") or inv.get("certification_status")
                       or inv.get("status") or ""),
            "buyer": inv.get("buyer_name", ""),
            "montant": inv.get("total_ttc", "0"),
        })
    return jsonify({
        "invoices": out,
        "count": len(out),
        "last_sfec": cache.get("last_sfec_sync_at", ""),
    })


@app.route("/api/retry-queue")
@_login_required
def api_retry_queue():
    return jsonify(get_retry_queue())


@app.route("/api/config", methods=["GET"])
@_login_required
def api_get_config():
    cfg = get_config()
    masked = dict(cfg)
    for section in ("db", "sfec"):
        if section in masked:
            masked[section] = dict(masked[section])
            for key in ("password", "api_key"):
                if key in masked[section]:
                    masked[section][key] = "***"
    return jsonify(masked)


# ── Config Save Endpoints ──

@app.route("/api/config/db", methods=["POST"])
@_login_required
def api_config_db():
    cfg = get_config()
    db = dict(cfg.get("db", {}))
    data = request.get_json(silent=True) or {}
    for key in ("server", "database", "user", "password"):
        if key in data:
            db[key] = data[key]
    for key in ("port", "polling_interval_ms", "pool_max", "pool_min",
                "pool_idle_timeout_ms", "connection_timeout_ms", "request_timeout_ms"):
        if key in data:
            try:
                db[key] = int(data[key])
            except (ValueError, TypeError):
                pass
    for key in ("trusted_connection", "encrypt", "trust_server_certificate", "polling_enabled"):
        if key in data:
            db[key] = _bool(data[key])
    cfg["db"] = db
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/sfec", methods=["POST"])
@_login_required
def api_config_sfec():
    cfg = get_config()
    sfec = dict(cfg.get("sfec", {}))
    data = request.get_json(silent=True) or {}
    for key in ("api_key", "base_url", "sandbox_url", "certify_status"):
        if key in data:
            sfec[key] = data[key]
    for key in ("use_sandbox", "enabled"):
        if key in data:
            sfec[key] = _bool(data[key])
    cfg["sfec"] = sfec
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/company", methods=["POST"])
@_login_required
def api_config_company():
    cfg = get_config()
    company = dict(cfg.get("company", {}))
    data = request.get_json(silent=True) or {}
    str_fields = ("name", "tax_number", "address", "city", "country", "phone", "email",
                  "website", "rc_number", "bank_name", "bank_account", "bank_iban", "bank_swift",
                  "currency", "slogan", "default_payment_terms",
                  "invoice_prefix", "invoice_notes", "invoice_footer", "tax_regime")
    for key in str_fields:
        if key in data:
            company[key] = data[key]
    if "invoice_next_number" in data:
        try:
            company["invoice_next_number"] = int(data["invoice_next_number"])
        except (ValueError, TypeError):
            pass
    for key in ("qrcode_enabled",):
        if key in data:
            company[key] = _bool(data[key])
    cfg["company"] = company
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/sync", methods=["POST"])
@_login_required
def api_config_sync():
    cfg = get_config()
    data = request.get_json(silent=True) or {}

    db = dict(cfg.get("db", {}))
    if "polling_enabled" in data:
        db["polling_enabled"] = _bool(data["polling_enabled"])
    if "polling_interval_ms" in data:
        try:
            db["polling_interval_ms"] = int(data["polling_interval_ms"])
        except (ValueError, TypeError):
            pass
    cfg["db"] = db

    queue = dict(cfg.get("queue", {}))
    for key in ("max_concurrent", "max_retries", "retry_base_delay_ms", "max_queue_size", "job_timeout_ms"):
        if key in data:
            try:
                queue[key] = int(data[key])
            except (ValueError, TypeError):
                pass
    cfg["queue"] = queue

    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/validation", methods=["POST"])
@_login_required
def api_config_validation():
    cfg = get_config()
    data = request.get_json(silent=True) or {}

    validation = dict(cfg.get("validation", {}))
    if "strict_mode" in data:
        validation["strict_mode"] = _bool(data["strict_mode"])
    if "tolerance_amount" in data:
        try:
            validation["tolerance_amount"] = float(data["tolerance_amount"])
        except (ValueError, TypeError):
            pass
    cfg["validation"] = validation

    rate_limit = dict(cfg.get("rate_limit", {}))
    for key in ("daily_max", "per_minute_max", "alert_threshold_percent"):
        if key in data:
            try:
                rate_limit[key] = int(data[key])
            except (ValueError, TypeError):
                pass
    cfg["rate_limit"] = rate_limit

    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/notifications", methods=["POST"])
@_login_required
def api_config_notifications():
    cfg = get_config()
    data = request.get_json(silent=True) or {}
    notifications = dict(cfg.get("notifications", {}))
    if "enabled" in data:
        notifications["enabled"] = _bool(data["enabled"])

    events = []
    event_keys = {
        "evt_synced": "invoice:synced",
        "evt_val_error": "invoice:validation_error",
        "evt_dlq": "queue:dlq_added",
        "evt_db_fail": "db:connection_failed",
        "evt_db_err": "db:polling_error",
    }
    for form_key, event_name in event_keys.items():
        if form_key in data and _bool(data[form_key]):
            events.append(event_name)
    notifications["events"] = events

    cfg["notifications"] = notifications
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/system", methods=["POST"])
@_login_required
def api_config_system():
    cfg = get_config()
    data = request.get_json(silent=True) or {}

    if "log_level" in data:
        cfg["log_level"] = data["log_level"]

    dash = dict(cfg.get("dashboard", {}))
    for key in ("login_email", "login_password", "session_secret", "host"):
        if key in data:
            dash[key] = data[key]
    if "port" in data:
        try:
            dash["port"] = int(data["port"])
        except (ValueError, TypeError):
            pass
    if "auth_enabled" in data:
        dash["auth_enabled"] = _bool(data["auth_enabled"])
    if "session_max_age_hours" in data:
        try:
            dash["session_max_age_hours"] = int(data["session_max_age_hours"])
        except (ValueError, TypeError):
            pass
    cfg["dashboard"] = dash

    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/dashboard", methods=["POST"])
@_login_required
def api_config_dashboard():
    return api_config_system()


@app.route("/api/config/reload", methods=["POST"])
@_login_required
def api_config_reload():
    try:
        result = apply_config()
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/config/export")
@_login_required
def api_config_export():
    from flask import send_file, after_this_request
    import tempfile
    cfg = get_config()
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(cfg, tmp, indent=2, ensure_ascii=False)
    tmp.close()
    tmp_path = tmp.name

    @after_this_request
    def cleanup(response):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return response

    return send_file(tmp_path, as_attachment=True, download_name="config.json", mimetype="application/json")


@app.route("/api/config/import", methods=["POST"])
@_login_required
def api_config_import():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Donnees JSON invalides (objet requis)"}), 400

    allowed_sections = {"db", "sfec", "company", "dashboard", "queue",
                        "validation", "notifications", "rate_limit", "log_level"}
    unknown = set(data.keys()) - allowed_sections
    if unknown:
        return jsonify({"ok": False, "error": "Sections inconnues: {}".format(", ".join(sorted(unknown)))}), 400

    required_sections = ("db", "sfec", "company")
    for section in required_sections:
        if section not in data:
            return jsonify({"ok": False, "error": "Section '{}' manquante".format(section)}), 400
        if not isinstance(data[section], dict):
            return jsonify({"ok": False, "error": "La section '{}' doit etre un objet".format(section)}), 400

    try:
        save_config(data)
        return jsonify({"ok": True, "message": "Configuration importee avec succes"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
# PHASE 3+4 : FACTURATION & POS
# ══════════════════════════════════════════════════════════════

@app.route("/billing")
@_login_required
def billing_page():
    stats = invoice_engine.count_invoices()
    bi_stats = sync_bidirectional.get_sync_stats()

    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) c FROM invoices WHERE sfec_statut IN ('CERTIFIE', 'DEJA_CERTIFIE')")
        stats["certifiees"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM invoices WHERE sfec_statut = 'EN_COURS'")
        stats["certif_en_cours"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM invoices WHERE sfec_statut = 'ERREUR'")
        stats["certif_erreur"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM invoices WHERE statut != 'brouillon' AND sfec_statut NOT IN ('CERTIFIE', 'DEJA_CERTIFIE')")
        stats["non_certifiees"] = cur.fetchone()["c"]
        cur.execute("""
            SELECT COUNT(*) c FROM invoices
            WHERE source IN ('web', 'pos') AND synced_sage = 0 AND statut != 'brouillon'
              AND (source != 'pos' OR (sfec_num_certif IS NOT NULL AND sfec_num_certif != ''))
        """)
        stats["pending_sage"] = cur.fetchone()["c"]
        cur.execute("SELECT COALESCE(SUM(montant_restant), 0) t, COUNT(*) c FROM invoices WHERE statut != 'brouillon' AND montant_restant > 0")
        m = cur.fetchone()
        stats["impayes_total"] = m["t"]
        stats["impayes_nb"] = m["c"]
        cur.execute("""
            SELECT COUNT(*) c, COALESCE(SUM(montant_ht), 0) ht, COALESCE(SUM(montant_tva), 0) tva,
                   COALESCE(SUM(montant_ttc), 0) ttc
            FROM invoices
            WHERE statut != 'brouillon' AND strftime('%Y-%m', substr(date_facture, 1, 10)) = strftime('%Y-%m', 'now')
        """)
        m = cur.fetchone()
        stats["mois_nb"] = m["c"]
        stats["mois_ht"] = m["ht"]
        stats["mois_tva"] = m["tva"]
        stats["mois_ttc"] = m["ttc"]

    statut_opts = ('<option value="">Tous</option><option value="brouillon">Brouillon</option>'
                   '<option value="valide">Validee</option><option value="a_comptabiliser">A comptabiliser</option>'
                   '<option value="a_comptabilise">A comptabilise</option>')
    source_opts = ('<option value="">Toutes</option><option value="web">Web</option>'
                   '<option value="pos">POS</option><option value="sage">Sage</option>')

    return _page(render_template("billing/list.html",
        total=stats.get("total", 0), brouillons=stats.get("brouillons", 0),
        validees=stats.get("validees", 0), certifiees=stats.get("certifiees", 0),
        ca=stats.get("ca_total", 0), pushed=bi_stats.get("pushed", 0),
        certif_en_cours=stats.get("certif_en_cours", 0), certif_erreur=stats.get("certif_erreur", 0),
        nb_imp=stats.get("impayes_total", 0), pending_sage=stats.get("pending_sage", 0),
        mois_nb=stats.get("mois_nb", 0), mois_ht=stats.get("mois_ht", 0),
        mois_tva=stats.get("mois_tva", 0), mois_ttc=stats.get("mois_ttc", 0),
        statut_opts=statut_opts, source_opts=source_opts,
        last_sync=bi_stats.get("last_sync", "jamais")[:19].replace("T", " ") if bi_stats.get("last_sync") else "jamais",
    ))


@app.route("/api/invoices/list")
@_login_required
def api_invoices_list():
    page = max(1, request.args.get("page", 1, type=int))
    limit = max(1, min(request.args.get("limit", 25, type=int), 200))
    res = invoice_engine.list_invoices(
        statut=request.args.get("statut") or None,
        source=request.args.get("source") or None,
        search=request.args.get("search") or None,
        date_from=request.args.get("date_from") or None,
        date_to=request.args.get("date_to") or None,
        limit=limit, offset=(page - 1) * limit,
        sort_by=request.args.get("sort_by") or "date_facture",
        sort_dir=request.args.get("sort_dir") or "DESC"
    )
    total = res.get("total", 0)
    pages = max(1, (total + limit - 1) // limit)
    return jsonify({"invoices": res.get("invoices", []), "total": total,
                    "page": page, "pages": pages, "limit": limit})


@app.route("/billing/invoice/new")
@_login_required
def invoice_new_page():
    return _invoice_form_page(None)


@app.route("/billing/invoice/<int:invoice_id>")
@_login_required
def invoice_detail_page(invoice_id):
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        return "<h1>Facture non trouvee</h1><p><a href='/billing'>Retour</a></p>", 404
    lignes_rows = ""
    for l in inv.get("lignes", []):
        lignes_rows += "<tr><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.2f}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.0f}</td><td style='text-align:right'>{:,.0f}</td><td style='text-align:right'>{:,.0f}</td></tr>".format(
            _esc(l.get("designation", "")), l.get("quantite", 1), l.get("prix_unitaire", 0),
            _esc("{}%".format(l.get("taux_tva", 18))), l.get("montant_ht", 0), l.get("montant_tva", 0), l.get("montant_ttc", 0)
        )
    ss = inv.get("sfec_statut", "")
    sfec_html = ""
    if ss:
        sfb = "badge-ok" if ss in ("CERTIFIE", "DEJA_CERTIFIE") else "badge-err" if ss == "ERREUR" else "badge-warn"
        sfec_html = '<tr><td>SFEC</td><td><span class="badge {}">{}</span></td></tr><tr><td>N Certif</td><td>{}</td></tr>'.format(sfb, _esc(ss), _esc(inv.get("sfec_num_certif", "")[:30] or "-"))
    return _page(render_template("billing/detail.html",
        numero=_esc(inv.get("numero", "")),
        inv_id=inv["id"],
        date_facture=_esc(inv.get("date_facture", "")),
        reference=_esc(inv.get("reference", "") or "-"),
        tiers_nom=_esc(inv.get("tiers_nom", "") or "N/A"),
        tiers_code=_esc(inv.get("tiers_code", "") or ""),
        statut=_esc(inv.get("statut", "")),
        source=_esc(inv.get("source", "web")),
        sfec_html=sfec_html,
        notes=_esc(inv.get("notes", "") or "-"),
        lignes_rows=lignes_rows,
        montant_ht=inv.get("montant_ht", 0),
        montant_tva=inv.get("montant_tva", 0),
        montant_ttc=inv.get("montant_ttc", 0),
    ))


@app.route("/billing/invoice/<int:invoice_id>/edit")
@_login_required
def invoice_edit_page(invoice_id):
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        return "<h1>Facture non trouvee</h1><p><a href='/billing'>Retour</a></p>", 404
    return _invoice_form_page(inv)


def _invoice_form_page(inv):
    is_edit = inv is not None
    title = "Modifier Facture" if is_edit else "Nouvelle Facture"
    numero = inv.get("numero", "") if is_edit else ""
    date_facture = inv.get("date_facture", "") if is_edit else ""
    date_echeance = inv.get("date_echeance", "") if is_edit else ""
    reference = inv.get("reference", "") if is_edit else ""
    tiers_code = inv.get("tiers_code", "") if is_edit else ""
    tiers_nom = inv.get("tiers_nom", "") if is_edit else ""
    tiers_niu = inv.get("tiers_niu", "") if is_edit else ""
    tiers_email = inv.get("tiers_email", "") if is_edit else ""
    tiers_telephone = inv.get("tiers_telephone", "") if is_edit else ""
    tiers_adresse = inv.get("tiers_adresse", "") if is_edit else ""
    tiers_type = inv.get("tiers_type", "business") if is_edit else "business"
    notes = inv.get("notes", "") if is_edit else ""
    statut = inv.get("statut", "brouillon") if is_edit else "brouillon"

    lignes_json = json.dumps(inv.get("lignes", [])) if is_edit else "[]"
    contacts_json = json.dumps(pos_engine.list_contacts(limit=200))
    products_json = json.dumps(pos_engine.list_products(limit=200))
    tax_rates_json = json.dumps(pos_engine.list_tax_rates())

    return _page(render_template("billing/form.html",
        title=title, numero=_esc(numero), date_facture=_esc(date_facture), date_echeance=_esc(date_echeance),
        reference=_esc(reference), tiers_code=_esc(tiers_code), tiers_nom=_esc(tiers_nom),
        tiers_niu=_esc(tiers_niu), tiers_email=_esc(tiers_email), tiers_telephone=_esc(tiers_telephone),
        tiers_adresse=_esc(tiers_adresse), notes=_esc(notes),
        s_brouillon="selected" if statut == "brouillon" else "",
        s_valide="selected" if statut == "valide" else "",
        s_acompt="selected" if statut == "a_comptabiliser" else "",
        t_bus="selected" if tiers_type == "business" else "",
        t_ind="selected" if tiers_type == "individual" else "",
        t_gov="selected" if tiers_type == "government" else "",
        t_for="selected" if tiers_type == "foreign" else "",
        readonly="readonly" if is_edit else "",
        contacts_json=contacts_json, products_json=products_json, tax_rates_json=tax_rates_json,
        lignes_json=lignes_json, is_edit_js="true" if is_edit else "false",
        inv_id_js=inv["id"] if is_edit else "null",
        btn_text="Modifier" if is_edit else "Enregistrer"
    ) + "\n")


# ── API INVOICES ──

@app.route("/api/invoices", methods=["GET"])
@_login_required
def api_list_invoices():
    result = invoice_engine.list_invoices(
        type_doc=request.args.get("type"),
        statut=request.args.get("statut"),
        source=request.args.get("source"),
        search=request.args.get("search"),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        limit=min(int(request.args.get("limit", 200)), 500),
        offset=int(request.args.get("offset", 0))
    )
    return jsonify(result)


@app.route("/api/invoices", methods=["POST"])
@_login_required
def api_create_invoice():
    data = request.get_json(silent=True) or {}
    try:
        result = invoice_engine.create_invoice(data)
        who = (_current_identity() or {}).get("email", "")
        user_auth.audit("facture.creee", "Facture {} creee".format(result.get("numero", "")), email=who)
        return jsonify({"success": True, "id": result["id"], "numero": result["numero"]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/invoices/<int:invoice_id>", methods=["GET"])
@_login_required
def api_get_invoice(invoice_id):
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        return jsonify({"error": "Non trouvee"}), 404
    return jsonify(inv)


@app.route("/api/invoices/<int:invoice_id>", methods=["PUT"])
@_login_required
def api_update_invoice(invoice_id):
    data = request.get_json(silent=True) or {}
    try:
        result = invoice_engine.update_invoice(invoice_id, data)
        if not result:
            return jsonify({"error": "Non trouvee"}), 404
        who = (_current_identity() or {}).get("email", "")
        user_auth.audit("facture.modifiee", "Facture {} modifiee".format(result.get("numero", "")), email=who)
        return jsonify({"success": True, "id": result["id"], "numero": result["numero"]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/invoices/<int:invoice_id>", methods=["DELETE"])
@_login_required
def api_delete_invoice(invoice_id):
    try:
        numero = ""
        inv = invoice_engine.get_invoice(invoice_id)
        if inv:
            numero = inv.get("numero", "")
        ok = invoice_engine.delete_invoice(invoice_id)
        if not ok:
            return jsonify({"error": "Non trouvee"}), 404
        who = (_current_identity() or {}).get("email", "")
        user_auth.audit("facture.supprimee", "Facture {} supprimee".format(numero), email=who)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/invoices/<int:invoice_id>/push-sage", methods=["POST"])
@_login_required
def api_invoice_push_sage(invoice_id):
    try:
        result = sync_bidirectional.push_invoice_to_sage(invoice_id)
        code = 200 if result.get("success") else 400
        if result.get("success"):
            who = (_current_identity() or {}).get("email", "")
            user_auth.audit("facture.poussee_sage", "Facture #{} poussee vers Sage".format(invoice_id), email=who)
        return jsonify(result), code
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/invoices/stats")
@_login_required
def api_invoice_stats():
    return jsonify(invoice_engine.count_invoices())


# ── SYNC BIDIRECTIONNELLE ──

@app.route("/api/sync/bi", methods=["POST"])
@_login_required
def api_sync_bi():
    try:
        result = sync_bidirectional.full_sync()
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sync/bi/stats")
@_login_required
def api_sync_bi_stats():
    return jsonify(sync_bidirectional.get_sync_stats())


# ── CONTACTS & PRODUCTS ──

@app.route("/api/contacts", methods=["GET"])
@_login_required
def api_list_contacts():
    contacts = pos_engine.list_contacts(
        type_filter=request.args.get("type"),
        search=request.args.get("search"),
        limit=min(int(request.args.get("limit", 200)), 500)
    )
    return jsonify({"contacts": contacts, "total": len(contacts)})


@app.route("/api/contacts", methods=["POST"])
@_login_required
def api_create_contact():
    data = request.get_json(silent=True) or {}
    try:
        result = pos_engine.create_contact(data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/products", methods=["GET"])
@_login_required
def api_list_products():
    products = pos_engine.list_products(
        type_filter=request.args.get("type"),
        search=request.args.get("search"),
        limit=min(int(request.args.get("limit", 200)), 500)
    )
    return jsonify({"products": products, "total": len(products)})


@app.route("/api/products", methods=["POST"])
@_login_required
def api_create_product():
    data = request.get_json(silent=True) or {}
    try:
        result = pos_engine.create_product(data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/tax-rates/local")
@_login_required
def api_local_tax_rates():
    return jsonify(pos_engine.list_tax_rates())


# ══════════════════════════════════════════════════════════════
# PHASE 4 : MODULE VENTE / POS
# ══════════════════════════════════════════════════════════════

@app.route("/pos")
@_login_required
def pos_page():
    stats = pos_engine.count_tickets()
    tickets_list = pos_engine.list_tickets(limit=8)
    current_vendeur_id = session.get("vendeur_id")
    vendeurs = pos_engine.list_vendeurs()
    top_products = pos_engine.top_selling_products(limit=10)

    def _pos_tile(p, rank=None):
        sold = int(p.get("total_vendu", 0) or 0)
        badge = '<span class="badge-sold">TOP {}</span>'.format(rank) if rank and sold > 0 else ""
        stock_val = int(round(p.get("stock_reel", 0) or 0))
        stock_cls = "out" if stock_val <= 0 else ("low" if stock_val < 5 else "")
        return ('<div class="pos-tile" data-id="{pid}" data-ref="{ref}" data-barcode="{bc}" '
                'data-des="{des}" data-prix="{prix}" data-tva="{tva}" '
                'onclick="addToCartFromTile(this)">{badge}'
                '<div class="pos-tile-nom">{des2}</div>'
                '<div class="pos-tile-ref">{ref2}</div>'
                '<div class="pos-tile-foot">'
                '<span class="pos-tile-prix">{prix2}</span>'
                '<span class="pos-tile-stock {stock_cls}">{stock}</span>'
                '</div>'
                '</div>').format(
                    pid=str(p["id"]), ref=_esc(p.get("ref", "")),
                    bc=_esc(p.get("barcode", "") or ""),
                    des=_esc(p.get("designation", "")),
                    prix=p.get("prix_vente", 0) or 0,
                    tva=_esc(p.get("tva_code", "18") or "18"),
                    badge=badge,
                    des2=_esc(p.get("designation", "")),
                    ref2=_esc(p.get("ref", "")),
                    prix2="{:,}".format(int(round(p.get("prix_vente", 0) or 0))),
                    stock_cls=stock_cls,
                    stock="{:,}".format(stock_val))

    top10_tiles = "".join(_pos_tile(p, i + 1) for i, p in enumerate(top_products))
    if not top10_tiles:
        top10_tiles = ('<div class="pos-results-empty">Aucun article disponible. '
                       'Utilisez <b>Sync articles</b> pour importer le catalogue Sage.</div>')

    ticket_rows_html = ""
    for t in tickets_list["tickets"]:
        ticket_rows_html += ('<div class="pos-last-row"><span class="num">{num}</span>'
                              '<span class="time">{time}</span>'
                              '<span class="amt">{amt} FCFA</span>'
                              '<span class="pay">{pay}</span></div>').format(
            num=_esc(t.get("numero", "")), time=_esc(t.get("date_ticket", "")[11:19] or ""),
            amt="{:,.0f}".format(t.get("montant_ttc", 0)),
            pay=_esc((t.get("mode_paiement", "") or "").replace("_", " "))
        )
    if not ticket_rows_html:
        ticket_rows_html = '<div class="pos-results-empty">Aucun ticket pour le moment</div>'

    vendeur_opts_html = '<option value="">Sans vendeur</option>'
    for v in vendeurs:
        label = "{} {}".format(v.get("prenom", "") or "", v.get("nom", "") or "").strip()
        sel = ' selected' if current_vendeur_id and v["id"] == current_vendeur_id else ""
        vendeur_opts_html += '<option value="{}"{}>{}</option>'.format(v["id"], sel, label)

    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT id, code, nom, niu FROM contacts WHERE type = 'client' AND est_actif = 1 ORDER BY nom")
        pos_clients = sqlite_db.rows_to_list(cur.fetchall())
    pos_clients_js = json.dumps([
        {"id": c["id"], "code": c["code"], "nom": c["nom"]} for c in pos_clients
    ], ensure_ascii=False)
    pos_clients_html = '<option value="|CLI-CPT|Client comptoir" selected>Client comptoir</option>'
    for c in pos_clients:
        pos_clients_html += '<option value="|{}|{}">{}</option>'.format(
            _esc(c["code"]), _esc(c["nom"]), _esc("{} - {}".format(c["code"], c["nom"])))

    stats_html = ('<div class="pos-stat-pill"><b>{tj}</b><span>Tickets aujourd&#39;hui</span></div>'
                  '<div class="pos-stat-pill"><b>{cj} FCFA</b><span>CA du jour</span></div>'
                  '<div class="pos-stat-pill"><b>{ct} FCFA</b><span>CA total</span></div>').format(
                      tj=str(stats.get("tickets_jour", 0)),
                      cj="{:,}".format(int(round(stats.get("ca_jour", 0)))),
                      ct="{:,}".format(int(round(stats.get("ca_total", 0)))))

    body = render_template("pos/index.html",
        stats_html=stats_html,
        top10_tiles=top10_tiles,
        vendeur_opts_html=vendeur_opts_html,
        pos_clients_html=pos_clients_html,
        pos_clients_js=pos_clients_js,
        ticket_rows_html=ticket_rows_html,
        stock_ctl_js="true" if pos_engine.stock_control_enabled() else "false",
    )
    return _page(body)


@app.route("/pos/ticket/<int:ticket_id>/print")
@_login_required
def pos_print_ticket(ticket_id):
    ticket = pos_engine.get_ticket(ticket_id)
    if not ticket:
        return "<h1>Ticket non trouve</h1>", 404

    company = get_config().get("company", {})
    lignes_rows = ""
    for l in ticket.get("lignes", []):
        lignes_rows += "<tr><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.0f}</td><td style='text-align:right'>{:,.0f}</td></tr>".format(
            _esc(l.get("designation", "")), l.get("quantite", 1),
            l.get("prix_unitaire", 0), l.get("montant_ttc", 0)
        )

    logo_html = ""
    logo_path = company.get("logo_file_name", "")
    if logo_path and os.path.exists(logo_path):
        logo_html = '<img src="{}" style="max-height:60px">'.format(logo_path)
    elif company.get("logo_base64"):
        logo_html = '<img src="data:image/png;base64,{}" style="max-height:60px">'.format(company["logo_base64"])

    def _fmt_cert_date(s):
        s = (s or "").strip()
        if not s:
            return ""
        return s.replace("T", " ").replace("Z", "").strip()[:19]

    company_meta = ""
    for line in [
        company.get("address", ""),
        "NIU: {}".format(company.get("tax_number", "")) if company.get("tax_number") else "",
        "RCCM: {}".format(company.get("rc_number", "")) if company.get("rc_number") else "",
        "Tel: {}".format(company.get("phone", "")) if company.get("phone") else "",
        company.get("email", ""),
    ]:
        if line:
            company_meta += '<div class="info">{}</div>'.format(_esc(line))

    invoice_html = ""
    sfec_html = ""
    invoice_id = ticket.get("invoice_id")
    if invoice_id:
        try:
            from app.domain import invoices as invoice_engine
            inv = invoice_engine.get_invoice(invoice_id)
            if inv:
                invoice_html = "<div><span>Facture</span><span><b>{}</b></span></div>".format(_esc(inv.get("numero", "")))
                cert_num = inv.get("sfec_num_certif", "") or ""
                qr = inv.get("sfec_qr_code", "") or ""
                cert_date = _fmt_cert_date(inv.get("sfec_date_certif", ""))
                sstatut = inv.get("sfec_statut", "") or ""
                if cert_num:
                    cert_short = cert_num if len(cert_num) <= 40 else cert_num[:40]
                    parts = ['<div class="info" style="font-weight:bold">Certifiee SFEC</div>',
                             '<div class="info">N certif : {}</div>'.format(_esc(cert_short))]
                    if cert_date:
                        parts.append('<div class="info">Date : {}</div>'.format(_esc(cert_date)))
                    if qr and (qr.startswith("data:") or qr.startswith("http")) and len(qr) < 40000:
                        parts.append('<div style="text-align:center;margin:2px 0">'
                                     '<img src="{}" style="width:16mm;height:16mm;image-rendering:pixelated"></div>'.format(qr))
                    sfec_html = '<div class="rule"></div>' + "".join(parts)
                elif sstatut == "ERREUR":
                    sfec_html = ('<div class="rule"></div>'
                                 '<div class="info" style="font-weight:bold">SFEC : ERREUR - certification a reessayer</div>')
                else:
                    sfec_html = ('<div class="rule"></div>'
                                 '<div class="info" style="font-weight:bold">SFEC : certification en attente...</div>')
        except Exception:
            pass

    paiement_label = ticket.get("mode_paiement", "")
    paiement_map = {"especes": "Especes", "mobile_money": "Mobile Money", "virement": "Virement",
                    "carte": "Carte", "cheque": "Cheque", "mixte": "Mixte"}
    paiement_label = paiement_map.get(paiement_label, paiement_label)

    return render_template("pos/ticket_print.html",
        numero=_esc(ticket.get("numero", "")),
        logo=logo_html,
        company_name=_esc(company.get("name", "Mon Entreprise")),
        company_meta=company_meta,
        date=_esc(ticket.get("date_ticket", "")),
        caissier=_esc(ticket.get("caissier", "")),
        client=_esc(ticket.get("tiers_nom", "")),
        invoice_html=invoice_html,
        sfec=sfec_html,
        lignes=lignes_rows,
        total="{:,.0f}".format(ticket.get("montant_ttc", 0)),
        paiement=_esc(paiement_label),
        recu="{:,.0f}".format(ticket.get("montant_recu", 0)),
        monnaie="{:,.0f}".format(ticket.get("monnaie_rendue", 0)),
        message=get_setting("ticket_message", "Merci pour votre achat !"),
        ticket_id=ticket["id"],
    )


# ── API POS ──

@app.route("/api/pos/config", methods=["GET", "POST"])
@_login_required
def api_pos_config():
    cfg = get_config()
    if request.method == "GET":
        return jsonify({"success": True, "stock_control": pos_engine.stock_control_enabled()})
    data = request.get_json(silent=True) or {}
    try:
        pos = cfg.get("pos", {}) or {}
        if "stock_control" in data:
            pos["stock_control"] = bool(data["stock_control"])
        cfg["pos"] = pos
        save_config(cfg)
        return jsonify({"success": True, "stock_control": bool(pos.get("stock_control", True))})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/pos/ticket", methods=["POST"])
@_login_required
def api_create_ticket():
    data = request.get_json(silent=True) or {}
    try:
        result = pos_engine.create_ticket(data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/pos/tickets", methods=["GET"])
@_login_required
def api_list_tickets():
    result = pos_engine.list_tickets(
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        vendeur_id=request.args.get("vendeur_id"),
        search=request.args.get("search"),
        limit=min(int(request.args.get("limit", 100)), 500)
    )
    return jsonify(result)


@app.route("/api/pos/ticket/<int:ticket_id>", methods=["GET"])
@_login_required
def api_get_ticket(ticket_id):
    ticket = pos_engine.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Non trouve"}), 404
    return jsonify(ticket)


@app.route("/api/pos/stats")
@_login_required
def api_pos_stats():
    return jsonify(pos_engine.count_tickets())


@app.route("/api/pos/products/search")
@_login_required
def api_search_products():
    q = request.args.get("q", "")
    return jsonify(pos_engine.search_products(q, limit=20))


def get_setting(key, default=""):
    try:
        return sqlite_db.get_setting(key, default)
    except Exception:
        return default


@app.route("/api/products/barcode")
@_login_required
def api_find_barcode():
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "code requis"}), 400
    p = pos_engine.find_product_by_barcode(code)
    if p:
        return jsonify({"found": True, "product": p})
    return jsonify({"found": False})


# ══════════════════════════════════════════════════════════════
# HISTORIQUE DES VENTES
# ══════════════════════════════════════════════════════════════

@app.route("/sales")
@_login_required
def sales_page():
    stats = pos_engine.count_tickets()

    ticket_rows = ""
    result = pos_engine.list_tickets(limit=200)
    for t in result["tickets"]:
        vendeur_txt = "{} {}".format(t.get("vendeur_prenom", "") or "", t.get("vendeur_nom", "") or "").strip() or "-"
        sstatut = t.get("sfec_statut", "") or ""
        inv_id = t.get("invoice_id")
        if sstatut in ("CERTIFIE", "DEJA_CERTIFIE"):
            sfec_cell = '<span class="badge badge-ok" title="{}">Certifiee</span>'.format(_esc((t.get("sfec_num_certif", "") or "")[:25]))
        elif sstatut == "EN_COURS":
            sfec_cell = '<span class="badge badge-warn">En attente</span>'
            if inv_id:
                sfec_cell += ' <button class="btn btn-sm" onclick="certifySales({})">Certifier</button>'.format(inv_id)
        elif sstatut == "ERREUR":
            sfec_cell = '<span class="badge badge-err">Erreur</span>'
            if inv_id:
                sfec_cell += ' <button class="btn btn-sm" onclick="certifySales({})">Certifier</button>'.format(inv_id)
        elif inv_id:
            sfec_cell = '<span class="badge badge-info">Non certifiee</span> <button class="btn btn-sm" onclick="certifySales({})">Certifier</button>'.format(inv_id)
        else:
            sfec_cell = "-"
        ticket_rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{:,.0f}</td><td>{}</td><td>{}</td><td>{}</td><td><a href='/pos/ticket/{}/print' class='btn btn-sm' target='_blank'>Voir</a></td></tr>".format(
            _esc(t.get("numero", "")), _esc(t.get("date_ticket", "")[:16]),
            _esc(t.get("tiers_nom", "")), t.get("montant_ttc", 0),
            _esc(t.get("mode_paiement", "")), _esc(vendeur_txt), sfec_cell, t.get("id", "")
        )

    vendeurs = pos_engine.list_vendeurs()
    vendeur_options = '<option value="">Tous</option>'
    for v in vendeurs:
        vendeur_options += '<option value="{}">{} {}</option>'.format(v["id"], v.get("prenom", ""), v["nom"])

    return _page(render_template("directory/sales.html",
        total=stats.get("total", 0), tickets_jour=stats.get("tickets_jour", 0),
        ca_jour=stats.get("ca_jour", 0), ca_total=stats.get("ca_total", 0),
        count=result.get("total", 0), rows=ticket_rows,
        vendeur_opts=vendeur_options
    ))


# ══════════════════════════════════════════════════════════════
# GESTION DES VENDEURS
# ══════════════════════════════════════════════════════════════

@app.route("/vendeurs")
@_login_required
def vendeurs_page():
    vendeurs = pos_engine.list_vendeurs(active_only=False)
    stats = pos_engine.get_vendeur_stats()

    rows = ""
    for v in vendeurs:
        s = "badge-ok" if v.get("est_actif") else "badge-err"
        stxt = "Actif" if v.get("est_actif") else "Inactif"
        vstat = next((x for x in stats if x["id"] == v["id"]), None)
        nb_ventes = vstat["nb_ventes"] if vstat else 0
        ca = vstat["ca_total"] if vstat else 0
        rows += "<tr><td>{}</td><td>{} {}</td><td>{}</td><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.0f}</td><td><span class='badge {}'>{}</span></td><td><button class='btn btn-sm' onclick='editVendeur({})'>Modifier</button></td></tr>".format(
            _esc(v.get("code", "")), _esc(v.get("prenom", "")), _esc(v.get("nom", "")),
            _esc(v.get("role", "")), _esc(v.get("telephone", "") or v.get("email", "") or "-"),
            nb_ventes, ca, s, _esc(stxt), v["id"]
        )

    return _page(render_template("directory/vendeurs.html",
        total=len(vendeurs),
        actifs=sum(1 for v in vendeurs if v.get("est_actif")),
        ca_jour=sum(s.get("ca_total", 0) for s in stats),
        rows=rows
    ))


@app.route("/api/vendeurs", methods=["GET"])
@_login_required
def api_list_vendeurs():
    vendeurs = pos_engine.list_vendeurs(active_only=False)
    return jsonify({"vendeurs": vendeurs, "total": len(vendeurs)})


@app.route("/api/vendeurs", methods=["POST"])
@_login_required
def api_create_vendeur():
    data = request.get_json(silent=True) or {}
    return jsonify(pos_engine.create_vendeur(data))


@app.route("/api/vendeurs/<int:vendeur_id>", methods=["GET"])
@_login_required
def api_get_vendeur(vendeur_id):
    v = pos_engine.get_vendeur(vendeur_id)
    if not v:
        return jsonify({"error": "Non trouve"}), 404
    return jsonify(v)


@app.route("/api/vendeurs/<int:vendeur_id>", methods=["PUT"])
@_login_required
def api_update_vendeur(vendeur_id):
    data = request.get_json(silent=True) or {}
    return jsonify(pos_engine.update_vendeur(vendeur_id, data))


@app.route("/api/vendeurs/<int:vendeur_id>", methods=["DELETE"])
@_login_required
def api_delete_vendeur(vendeur_id):
    return jsonify(pos_engine.update_vendeur(vendeur_id, {"est_actif": 0}))


@app.route("/api/invoices/<int:invoice_id>/pdf")
@_login_required
def api_invoice_pdf(invoice_id):
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        return jsonify({"error": "Facture non trouvee"}), 404

    if not pdf_generator.HAS_REPORTLAB:
        return jsonify({"error": "reportlab non installe - pip install reportlab"}), 500

    try:
        pdf_bytes = pdf_generator.generate_invoice_pdf(inv)
        if not pdf_bytes:
            return jsonify({"error": "Erreur generation PDF"}), 500

        from flask import send_file
        buf = __import__("io").BytesIO(pdf_bytes)
        numero = inv.get("numero", "facture").replace("/", "-")
        return send_file(buf, as_attachment=True,
                         download_name="{}.pdf".format(numero),
                         mimetype="application/pdf")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pos/ticket/<int:ticket_id>/pdf")
@_login_required
def api_ticket_pdf(ticket_id):
    ticket = pos_engine.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket non trouve"}), 404

    if not pdf_generator.HAS_REPORTLAB:
        return jsonify({"error": "reportlab non installe - pip install reportlab"}), 500

    try:
        pdf_bytes = pdf_generator.generate_ticket_pdf(ticket)
        if not pdf_bytes:
            return jsonify({"error": "Erreur generation PDF"}), 500

        from flask import send_file
        buf = __import__("io").BytesIO(pdf_bytes)
        numero = ticket.get("numero", "ticket").replace("/", "-")
        return send_file(buf, as_attachment=True,
                         download_name="{}.pdf".format(numero),
                         mimetype="application/pdf")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/contacts/<int:contact_id>", methods=["PUT"])
@_login_required
def api_update_contact(contact_id):
    data = request.get_json(silent=True) or {}
    try:
        with sqlite_db.get_cursor() as cur:
            fields = []
            vals = []
            for k in ("nom", "type", "email", "telephone", "niu", "adresse", "ville", "pays", "est_actif"):
                if k in data:
                    fields.append("{} = ?".format(k))
                    vals.append(data[k])
            if not fields:
                return jsonify({"success": False, "error": "Aucun champ a modifier"})
            fields.append("updated_at = datetime('now')")
            vals.append(contact_id)
            cur.execute("UPDATE contacts SET {} WHERE id = ?".format(", ".join(fields)), vals)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/contacts/<int:contact_id>", methods=["DELETE"])
@_login_required
def api_delete_contact(contact_id):
    try:
        with sqlite_db.get_cursor() as cur:
            cur.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/clients")
@_login_required
def clients_page():
    contacts = pos_engine.list_contacts(limit=500)
    client_count = 0
    fourni_count = 0
    for c in contacts:
        if c.get("type") == "client":
            client_count += 1
        else:
            fourni_count += 1

    rows = ""
    for c in contacts:
        s = "badge-ok" if c.get("est_actif") else "badge-err"
        stxt = "Actif" if c.get("est_actif") else "Inactif"
        typ = c.get("type", "client")
        tc = "badge-info" if typ == "fournisseur" else "badge-ok"
        niu = c.get("niu", "") or "-"
        rows += """<tr>
            <td>{code}</td><td><span class="badge {tc}">{typ}</span></td>
            <td>{nom}</td><td>{niu}</td><td>{email}</td><td>{tel}</td><td>{ville}</td>
            <td><span class="badge {s}">{stxt}</span></td>
            <td><button class="btn btn-sm btn-primary" onclick="editContact({cid})">Editer</button>
            <button class="btn btn-sm btn-danger" onclick="deleteContact({cid})">Suppr</button></td>
        </tr>""".format(
            code=c.get("code",""), tc=tc, typ=typ.upper(),
            nom=c.get("nom",""), niu=niu, email=c.get("email",""), tel=c.get("telephone",""), ville=c.get("ville",""),
            s=s, stxt=stxt,
            cid=c["id"]
        )

    return render_template("directory/clients.html",
        css=CSS, nav=_sidebar(request.path),
        total=len(contacts), nb_clients=client_count, nb_fournis=fourni_count, nb_total=len(contacts),
        rows=rows,
        contacts_json=json.dumps(contacts),
        csrf_json=json.dumps(_get_csrf_token()),
        toast=ALERT_ZONE
    )


# ══════════════════════════════════════════════════════════════
# GESTION UTILISATEURS & DROITS D'ACCES
# ══════════════════════════════════════════════════════════════


def _user_rows(comptes, me_id):
    rows = ""
    role_cls = {"admin": "badge-ok", "responsable": "badge-info", "caissiere": "badge-warn", "financiere": ""}
    for u in comptes:
        rows += """<tr>
<td>{nom}</td><td>{email}</td>
<td><span class="badge {rc}">{role_label}</span>{me}</td>
<td><span class="badge {sc}">{s}</span></td>
<td>{derniere}</td>
<td>
<button class="btn btn-sm" onclick="editUser({uid})">Editer</button>
<button class="btn btn-sm" onclick="resetPwd({uid})">MdP</button>
<button class="btn btn-sm" onclick="toggleActif({uid})">{tgl}</button>
<button class="btn btn-sm btn-danger" onclick="delUser({uid})">Suppr</button>
</td></tr>""".format(
            nom=_esc("{} {}".format(u.get("prenom", ""), u.get("nom", "")).strip()),
            email=_esc(u["email"]),
            rc=role_cls.get(u["role"], ""), role_label=_esc(u["role_label"]),
            me=' <span class="badge badge-info">vous</span>' if u["id"] == me_id else "",
            sc="badge-ok" if u["est_actif"] else "badge-err",
            s="Actif" if u["est_actif"] else "Inactif",
            derniere=_esc((u.get("derniere_connexion") or "-")[:19]),
            uid=u["id"], tgl="Desactiver" if u["est_actif"] else "Activer",
        )
    return rows


def _matrix_editor(matrix):
    editor = "<table id='matrix'><thead><tr><th>Permission</th>"
    for r in user_auth.ROLES:
        editor += "<th>{}</th>".format(user_auth.ROLE_LABELS[r])
    editor += "</tr></thead><tbody>"
    for perm, label in user_auth.PERMISSIONS.items():
        editor += "<tr><td>{}</td>".format(_esc(label))
        for r in user_auth.ROLES:
            checked = "checked" if perm in (matrix.get(r) or []) else ""
            editor += ('<td style="text-align:center"><input type="checkbox" data-role="{}" '
                       'data-perm="{}" {}></td>').format(r, perm, checked)
        editor += "</tr>"
    return editor + "</tbody></table>"


def _audit_rows(entries):
    rows = ""
    for e in entries[:12]:
        rows += "<tr><td>{}</td><td>{}</td><td><span class='badge badge-info'>{}</span></td><td>{}</td><td>{}</td></tr>".format(
            _esc((e["ts"] or "")[2:16]), _esc(e.get("utilisateur", "")),
            _esc(e.get("action", "")), _esc(e.get("detail", "")), _esc(e.get("ip", "")))
    return rows or '<tr><td colspan="5" style="text-align:center;color:#94a3b8">Aucune activite</td></tr>'


@app.route("/utilisateurs")
@_login_required
def utilisateurs_page():
    comptes = user_auth.list_users()
    matrix = user_auth.get_matrix()
    audit = user_auth.list_audit(limit=20)
    me = _current_identity() or {}
    acfg = user_auth.get_auth_config()
    role_opts = "".join('<option value="{}">{}</option>'.format(r, user_auth.ROLE_LABELS[r]) for r in user_auth.ROLES)

    return _page(render_template("directory/utilisateurs.html",
        nb=str(len(comptes)),
        activ=str(sum(1 for u in comptes if u["est_actif"])),
        admins=str(sum(1 for u in comptes if u["role"] == "admin" and u["est_actif"])),
        matrix=_matrix_editor(matrix),
        audit=_audit_rows(audit),
        provider=acfg.get("provider", "local"),
        role_opts=role_opts,
    ))


@app.route("/compte/mot-de-passe")
@_login_required
def compte_password_page():
    return _page(render_template("auth/password.html"))


@app.route("/api/utilisateurs", methods=["GET"])
@_login_required
def api_list_comptes():
    return jsonify({"comptes": user_auth.list_users()})


@app.route("/api/utilisateurs", methods=["POST"])
@_login_required
def api_create_compte():
    data = request.get_json(silent=True) or {}
    try:
        new_id = user_auth.create_user(
            data.get("nom", ""), data.get("prenom", ""), data.get("email", ""),
            data.get("password", ""), data.get("role", "responsable"))
        return jsonify({"success": True, "id": new_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/utilisateurs/<int:user_id>", methods=["PUT"])
@_login_required
def api_update_compte(user_id):
    data = request.get_json(silent=True) or {}
    try:
        if data.get("toggle_actif"):
            u = user_auth.get_user(user_id)
            if not u:
                return jsonify({"success": False, "error": "Compte introuvable"}), 404
            user_auth.update_user(user_id, est_actif=not u["est_actif"])
            return jsonify({"success": True})
        user_auth.update_user(
            user_id,
            nom=data.get("nom"), prenom=data.get("prenom"),
            role=data.get("role"), est_actif=data.get("est_actif"))
        if data.get("password"):
            user_auth.set_password(user_id, data["password"],
                                   resetter=_current_identity().get("email", ""))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/utilisateurs/<int:user_id>", methods=["DELETE"])
@_login_required
def api_delete_compte(user_id):
    try:
        user_auth.delete_user(user_id, acting_user_id=(_current_identity() or {}).get("user_id"))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/utilisateurs/<int:user_id>/password", methods=["POST"])
@_login_required
def api_reset_compte_password(user_id):
    data = request.get_json(silent=True) or {}
    try:
        user_auth.set_password(user_id, data.get("password", ""),
                               resetter=(_current_identity() or {}).get("email", ""))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/compte/password", methods=["POST"])
@_login_required
def api_compte_password():
    ident = _current_identity() or {}
    data = request.get_json(silent=True) or {}
    try:
        user, err = user_auth.authenticate(ident.get("email", ""), data.get("current", ""))
        if not user:
            return jsonify({"success": False, "error": "Mot de passe actuel incorrect"}), 400
        user_auth.set_password(user["id"], data.get("nouveau", ""))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/auth/permissions", methods=["GET", "POST"])
@_login_required
def api_auth_permissions():
    if request.method == "GET":
        return jsonify({"roles": user_auth.ROLES, "roles_labels": user_auth.ROLE_LABELS,
                        "permissions": user_auth.PERMISSIONS,
                        "matrix": user_auth.get_matrix(),
                        "defaults": user_auth.DEFAULT_MATRIX})
    data = request.get_json(silent=True) or {}
    try:
        matrix = user_auth.set_matrix(data.get("matrix", {}))
        audit_entry = _current_identity().get("email", "")
        user_auth.audit("droits.modifies", "Matrice des droits mise a jour", email=audit_entry)
        return jsonify({"success": True, "matrix": matrix})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/auth/config", methods=["GET", "POST"])
@_login_required
def api_auth_config():
    if request.method == "GET":
        return jsonify({"config": user_auth.get_auth_config()})
    data = request.get_json(silent=True) or {}
    try:
        cfg = user_auth.save_auth_config(data)
        _apply_session_security()
        login_email = _current_identity() and _current_identity().get("email", "")
        user_auth.audit("securite.modifiee", "Parametres de securite mis a jour", email=login_email or "")
        return jsonify({"success": True, "config": cfg})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/csrf")
def api_csrf():
    return jsonify({"token": _get_csrf_token()})


@app.route("/api/audit", methods=["GET"])
@_login_required
def api_audit():
    return jsonify({"entries": user_auth.list_audit(limit=min(int(request.args.get("limit", 200)), 500))})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/ready")
def ready():
    return jsonify({"ready": True})

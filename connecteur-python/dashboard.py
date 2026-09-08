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
    Flask, render_template_string, request, jsonify,
    session, redirect, url_for
)
from config_manager import get_config, save_config
import user_auth
from sync_engine import (
    get_cache, get_metrics, sync_all, certify_single, sync_sfec_invoices,
    apply_config, get_retry_queue
)
from database import (
    ping_database, fetch_contacts, fetch_tax_rates,
    fetch_ledger_accounts, fetch_certified_invoices, list_all_tables,
    mark_to_monitor
)
from sfec_endpoints import check_health, preview_sfec_payload, validate_sfec_payload, certify_sqlite_invoice
from sfec_client import SfecClient
from connectivity import get_status, check_now
import sqlite_db
import invoice_engine
import pos_engine
import sage_writer
import sync_bidirectional
import pdf_generator

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
    from user_auth import get_auth_config
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
    body = (_DENY_HTML.replace("{perm}", _esc(perm or "")).replace("{label}", _esc(label or ""))
            .replace("{email}", _esc(session.get("user_email", ""))))
    if request.path.startswith("/api/"):
        return jsonify({"error": "Acces refuse (droits insuffisants)", "permission": perm}), 403
    return render_template_string(body), 403


_DENY_HTML = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>Acces refuse</title>
<style>
body{font-family:'Segoe UI',Tahoma,sans-serif;background:#f1f5f9;min-height:100vh;display:flex;align-items:center;justify-content:center;color:#1e293b}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:40px;text-align:center;max-width:420px;box-shadow:0 10px 30px rgba(15,23,42,0.06)}
.ico{width:56px;height:56px;margin:0 auto 16px;border-radius:14px;background:#fef2f2;color:#b91c1c;display:flex;align-items:center;justify-content:center;font-size:26px}
h1{font-size:20px;margin-bottom:8px}.sub{font-size:13px;color:#64748b;margin-bottom:20px}
.btn{display:inline-block;background:#4f46e5;color:#fff;text-decoration:none;padding:11px 22px;border-radius:10px;font-size:14px;font-weight:600}
</style></head><body><div class="card"><div class="ico">&#128274;</div>
<h1>Acces refuse</h1><p class="sub">Vous n'avez pas la permission <b>{label}</b> sur cette section.<br>Compte : {email}</p>
<a class="btn" href="/">Retour au tableau de bord</a></div></body></html>"""


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
            return render_template_string(
                _DENY_HTML.replace("{perm}", "csrf").replace("{label}", "Jeton de securite invalide")
                .replace("{email}", _esc(session.get("user_email", "")))
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


CSS = """*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',Tahoma,sans-serif;background:#f1f5f9;color:#1e293b;min-height:100vh;display:flex}
.sidebar{width:250px;background:#ffffff;border-right:1px solid #e2e8f0;padding:0;position:fixed;top:0;left:0;bottom:0;overflow-y:auto;z-index:100;box-shadow:2px 0 16px rgba(15,23,42,0.05);display:flex;flex-direction:column;transition:width 0.25s ease}
.sidebar.collapsed{width:76px}
.sidebar-top{display:flex;align-items:center;justify-content:space-between;padding:18px 16px 14px;border-bottom:1px solid #eef2f7}
.sidebar-logo{display:flex;align-items:center;gap:10px;text-decoration:none}
.sidebar-logo .icon{width:34px;height:34px;flex-shrink:0;border-radius:10px;background:linear-gradient(135deg,#4f46e5,#7c3aed);display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:14px;box-shadow:0 4px 10px rgba(79,70,229,0.3)}
.sidebar-logo span{font-size:15px;font-weight:700;color:#1e293b;letter-spacing:-0.3px;white-space:nowrap;transition:opacity 0.2s}
.sidebar-toggle{width:32px;height:32px;flex-shrink:0;border:none;border-radius:8px;background:#f1f5f9;color:#64748b;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all 0.18s}
.sidebar-toggle:hover{background:#e2e8f0;color:#1e293b}
.sidebar-toggle svg{width:18px;height:18px}
.sidebar-nav{list-style:none;padding:8px 0;margin:0;flex:1;overflow-y:auto}
.sidebar.collapsed .sidebar-logo span,
.sidebar.collapsed .sidebar-nav .nav-label,
.sidebar.collapsed .sidebar-nav li a .label,
.sidebar.collapsed .sidebar-foot .net-indicator span:not(.net-dot){display:none}
.sidebar.collapsed .sidebar-nav li a{justify-content:center;padding:11px 0;overflow:hidden}
.sidebar.collapsed .sidebar-nav li a .icon{margin:0}
.sidebar.collapsed .sidebar-top{justify-content:center;padding:18px 0 14px}
.sidebar.collapsed .sidebar-toggle{display:none}
.sidebar.collapsed + .main-content{margin-left:76px;max-width:calc(100vw - 76px)}
.sidebar-nav li a .label{white-space:nowrap}
.sidebar.collapsed:hover{width:250px;box-shadow:8px 0 28px rgba(15,23,42,.16),2px 0 8px rgba(15,23,42,.08)}
.sidebar.collapsed:hover .sidebar-top{justify-content:space-between;padding:18px 16px 14px}
.sidebar.collapsed:hover .sidebar-toggle{display:flex}
.sidebar.collapsed:hover .sidebar-logo span{display:inline-block}
.sidebar.collapsed:hover .sidebar-nav .nav-label{display:block}
.sidebar.collapsed:hover .sidebar-nav li a .label{display:inline}
.sidebar.collapsed:hover .sidebar-foot .net-indicator span:not(.net-dot){display:inline}
.sidebar.collapsed:hover .sidebar-nav li a{justify-content:flex-start;padding:10px 14px;overflow:visible}
.sidebar.collapsed:hover .sidebar-nav li a .icon{margin:0}
.sidebar-foot{position:sticky;bottom:0;background:#ffffff;padding:14px 20px;border-top:1px solid #eef2f7}
.net-indicator{display:flex;align-items:center;gap:8px;font-size:12px;color:#64748b}
.net-dot{width:10px;height:10px;border-radius:50%;display:inline-block;flex-shrink:0}
.net-dot.on{background:#10b981;box-shadow:0 0 0 3px rgba(16,185,129,0.2)}
.net-dot.off{background:#ef4444;box-shadow:0 0 0 3px rgba(239,68,68,0.2)}
.net-dot.unknown{background:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,0.2)}
.sidebar-nav{list-style:none;padding:0;margin:0}
.sidebar-nav li{margin:2px 8px 2px 10px}
.sidebar-nav li a{display:flex;align-items:center;gap:12px;padding:10px 14px;color:#64748b;text-decoration:none;font-size:13px;font-weight:500;transition:all 0.18s;border-radius:10px;border-left:3px solid transparent}
.sidebar-nav li a:hover{background:#f1f5f9;color:#1e293b}
.sidebar-nav li a.active{background:linear-gradient(90deg,rgba(79,70,229,0.12),rgba(124,58,237,0.06));color:#4f46e5;border-left-color:#4f46e5;font-weight:600;box-shadow:inset 0 0 0 1px rgba(79,70,229,0.08)}
.sidebar-nav .icon{width:22px;height:22px;display:flex;align-items:center;justify-content:center;font-size:15px;opacity:0.75;transition:all 0.18s}
.sidebar-nav .icon svg{width:18px;height:18px}
.sidebar-nav li a:hover .icon,.sidebar-nav li a.active .icon{opacity:1}
.sidebar-nav .nav-label{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#94a3b8;padding:16px 22px 6px}
.sidebar-nav li a .logout{color:#dc2626}
.sidebar-nav li a.logout-link{color:#dc2626}
.sidebar-nav li a.logout-link:hover{background:#fef2f2;color:#b91c1c}
.main-content{margin-left:250px;flex:1;padding:24px 32px;max-width:calc(100vw - 250px);transition:margin-left 0.25s ease}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid #eef2f7}
.topbar-title{font-size:21px;font-weight:700;color:#0f172a}
.topbar-actions{display:flex;align-items:center;gap:10px}
.user-chip{display:inline-flex;align-items:center;gap:8px;background:#fff;border:1px solid #e2e8f0;border-radius:999px;padding:6px 14px;font-size:12px;font-weight:600;color:#334155}
.user-chip .ava{width:26px;height:26px;border-radius:50%;background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700}
.page-header{margin-bottom:24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
.page-header h1{font-size:22px;font-weight:700;color:#1e293b;margin:0}
.breadcrumb{font-size:12px;color:#64748b;margin-bottom:4px}
.breadcrumb a{color:#4f46e5;text-decoration:none;font-weight:500}
.breadcrumb a:hover{text-decoration:underline}
.card{background:#fff;border-radius:14px;border:1px solid #e2e8f0;padding:20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(15,23,42,0.04)}
.card h2{font-size:15px;font-weight:600;color:#1e293b;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.card h2::before{content:'';display:inline-block;width:4px;height:18px;background:linear-gradient(180deg,#4f46e5,#7c3aed);border-radius:2px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:16px}
.stat{background:#fff;border-radius:12px;padding:18px;border:1px solid #e2e8f0;transition:all 0.2s;position:relative;overflow:hidden}
.stat::after{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#4f46e5,#7c3aed);opacity:0;transition:opacity 0.2s}
.stat:hover{box-shadow:0 8px 20px rgba(15,23,42,0.07);transform:translateY(-3px);border-color:#cbd5e1}
.stat:hover::after{opacity:1}
.stat .value{font-size:26px;font-weight:700;color:#0f172a;margin-bottom:4px;letter-spacing:-0.5px}
.stat .label{font-size:12px;color:#64748b;font-weight:500}
.stat .icon-chip{width:38px;height:38px;border-radius:10px;display:flex;align-items:center;justify-content:center;margin-bottom:12px}
.stat .icon-chip svg{width:19px;height:19px}
.badge{display:inline-block;padding:3px 10px;border-radius:6px;font-size:11px;font-weight:600}
.badge-ok{background:#dcfce7;color:#166534}
.badge-err{background:#fee2e2;color:#991b1b}
.badge-warn{background:#fef3c7;color:#92400e}
.badge-info{background:#dbeafe;color:#1e40af}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#f8fafc;color:#64748b;text-align:left;padding:10px 12px;border-bottom:2px solid #e2e8f0;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;font-weight:600}
td{padding:10px 12px;border-bottom:1px solid #f1f5f9;color:#334155}
tr:hover{background:#f8fafc}
label{display:block;color:#475569;font-size:11px;font-weight:600;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px}
input[type=text],input[type=password],input[type=number],input[type=email],input[type=date],select,textarea{
  background:#fff;border:1.5px solid #e2e8f0;color:#1e293b;
  padding:9px 12px;border-radius:8px;width:100%;font-size:13px;
  font-family:inherit;transition:all 0.2s;
}
input:focus,select:focus,textarea:focus{
  outline:none;border-color:#4f46e5;
  box-shadow:0 0 0 3px rgba(79,70,229,0.1);
  background:#fff;
}
input::placeholder{color:#94a3b8}
select{cursor:pointer;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2364748b' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 12px center;padding-right:36px}
textarea{resize:vertical;min-height:60px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:8px 16px;border-radius:8px;border:none;cursor:pointer;font-size:13px;font-weight:600;transition:all 0.15s;text-decoration:none}
.btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,0.1)}
.btn:active{transform:translateY(0)}
.btn-primary{background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;box-shadow:0 2px 8px rgba(79,70,229,0.25)}
.btn-primary:hover{box-shadow:0 4px 16px rgba(79,70,229,0.35)}
.btn-success{background:linear-gradient(135deg,#10b981,#059669);color:#fff;box-shadow:0 2px 8px rgba(16,185,129,0.25)}
.btn-danger{background:#ef4444;color:#fff}
.btn-warning{background:#f59e0b;color:#fff}
.btn-sm{padding:5px 12px;font-size:11px;border-radius:7px}
.btn-ghost{background:transparent;color:#64748b;border:1px solid #e2e8f0}
.btn-ghost:hover{background:#f1f5f9;color:#1e293b;border-color:#cbd5e1;box-shadow:none}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:768px){.grid-2{grid-template-columns:1fr}}
@media print{.no-print{display:none!important}body{background:#fff;color:#000}}
.tab-btn:hover{color:#4f46e5;background:#f1f5f9}
.tab-btn.active{background:#fff;color:#4f46e5;border-color:#4f46e5;border-bottom:1px solid #fff}
.tab-content{display:none;background:#fff;border:1px solid #e2e8f0;border-radius:0 8px 8px 8px;padding:20px;margin-bottom:16px}
.tab-content.active{display:block}
.alert-zone{position:fixed;top:64px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:10px;max-width:420px;min-width:260px;width:max-content}
.alert-card{display:flex;align-items:flex-start;gap:10px;padding:12px 14px;border-radius:10px;font-size:13px;font-weight:600;box-shadow:0 4px 14px rgba(15,23,42,0.18);border:1px solid;animation:alertIn .22s ease-out}
.alert-card .alert-icon{flex:none;margin-top:1px;width:18px;height:18px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#fff}
.alert-card .alert-msg{flex:1;line-height:1.45;word-break:break-word}
.alert-card .alert-x{cursor:pointer;flex:none;border:0;background:transparent;font-size:16px;line-height:1;opacity:.5;color:inherit;padding:2px;margin-left:2px}
.alert-card .alert-x:hover{opacity:1}
@keyframes alertIn{from{opacity:0;transform:translateX(14px)}to{opacity:1;transform:none}}
.alert-card.alert-out{opacity:0;transform:translateX(14px);transition:opacity .25s,transform .25s}
.alert-ok{border-color:#86efac;background:#f0fdf4;color:#166534}
.alert-ok .alert-icon{background:#22c55e}
.alert-err{border-color:#fca5a5;background:#fef2f2;color:#991b1b}
.alert-err .alert-icon{background:#ef4444}
.alert-warn{border-color:#fcd34d;background:#fffbeb;color:#92400e}
.alert-warn .alert-icon{background:#f59e0b}
.alert-info{border-color:#93c5fd;background:#eff6ff;color:#1e40af}
.alert-info .alert-icon{background:#3b82f6}
.alert-overlay{position:fixed;inset:0;z-index:10000;background:rgba(15,23,42,0.55);display:flex;align-items:center;justify-content:center;padding:16px;animation:fadeIn .18s ease-out;backdrop-filter:blur(2px)}
.alert-overlay.alert-out{opacity:0;transition:opacity .2s}
.alert-dialog{background:#fff;border-radius:14px;box-shadow:0 20px 60px rgba(15,23,42,0.45);width:100%;max-width:460px;border:1px solid #e2e8f0;overflow:hidden;animation:dlgIn .2s ease-out;text-align:left}
.alert-dialog.alert-out{transform:scale(.96);transition:transform .2s}
.alert-dialog-head{display:flex;align-items:center;gap:10px;padding:16px 18px 0;font-weight:700;font-size:15px;color:#0f172a}
.alert-dialog-icon{flex:none;width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;color:#fff;background:#64748b}
.alert-dialog-msg{padding:12px 18px 4px;font-size:13px;color:#334155;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.alert-dialog-actions{display:flex;justify-content:flex-end;gap:8px;padding:16px 18px 18px}
@keyframes dlgIn{from{opacity:0;transform:translateY(8px) scale(.97)}to{opacity:1;transform:none}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.alert-dialog.alert-err .alert-dialog-icon{background:#ef4444}
.alert-dialog.alert-warn .alert-dialog-icon{background:#f59e0b}
.alert-dialog.alert-ok .alert-dialog-icon{background:#22c55e}
.alert-dialog.alert-info .alert-dialog-icon{background:#3b82f6}
.net-indicator{display:flex;align-items:center;gap:6px;font-size:12px;color:#64748b}
.form-row{margin-bottom:8px}
.desc{font-size:11px;color:#64748b;margin-top:2px}
.queue-badge{background:#ede9fe;color:#5b21b6;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.invoice-layout{display:grid;grid-template-columns:1fr 360px;gap:20px;align-items:start}
@media(max-width:1024px){.invoice-layout{grid-template-columns:1fr}}
.invoice-main{min-width:0}
.invoice-sidebar{position:sticky;top:24px}
.section-title{font-size:13px;font-weight:700;color:#4f46e5;text-transform:uppercase;letter-spacing:0.8px;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.section-title::before{content:'';display:inline-block;width:4px;height:16px;background:linear-gradient(180deg,#4f46e5,#7c3aed);border-radius:2px}
.invoice-meta{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
@media(max-width:768px){.invoice-meta{grid-template-columns:1fr 1fr}}
.invoice-meta .full-width{grid-column:1/-1}
.line-item{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px;margin-bottom:10px;transition:all 0.2s;box-shadow:0 1px 2px rgba(0,0,0,0.04)}
.line-item:hover{border-color:#cbd5e1;box-shadow:0 4px 12px rgba(0,0,0,0.08)}
.line-item-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.line-item-title{font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.5px}
.line-item-grid{display:grid;grid-template-columns:2fr 1fr 1fr 1fr auto;gap:10px;align-items:end}
@media(max-width:768px){.line-item-grid{grid-template-columns:1fr 1fr;gap:8px}.line-item-grid .full-width-mobile{grid-column:1/-1}}
.line-item-actions{display:flex;gap:6px;align-items:center}
.totals-panel{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:20px;position:sticky;top:24px;box-shadow:0 1px 3px rgba(0,0,0,0.04)}
.totals-panel h3{color:#1e293b;font-size:14px;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid #e2e8f0}
.niu-required input{border-color:#f59e0b!important;background:#fffbeb!important}
.niu-required .niu-star{color:#f59e0b;font-weight:700}
.niu-required-hint{color:#b45309;font-size:11px;margin-top:3px}
.niu-opt-hint{color:#64748b;font-size:11px;margin-top:3px}
.tiers-type-info{margin-top:6px;font-size:11px;color:#4f46e5}
.contact-select-wrapper{position:relative}
.total-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;font-size:13px;color:#64748b}
.total-row.grand-total{border-top:2px solid #e2e8f0;margin-top:8px;padding-top:12px;font-size:18px;font-weight:700;color:#1e293b}
.total-row .total-label{font-weight:600;text-transform:uppercase;letter-spacing:0.5px;font-size:11px}
.total-row .total-value{font-weight:700;color:#1e293b}
.form-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px;padding-top:16px;border-top:1px solid #e2e8f0}
.form-actions .btn{flex:1;min-width:140px;justify-content:center;display:inline-flex;align-items:center;justify-content:center;padding:12px 20px;font-size:14px}
@media(max-width:768px){.form-actions .btn{min-width:100%}}
.page-header{margin-bottom:24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
.page-header h1{font-size:22px;font-weight:700;color:#1e293b;margin:0}
.breadcrumb{font-size:12px;color:#64748b;margin-bottom:4px}
.breadcrumb a{color:#4f46e5;text-decoration:none;font-weight:500}
.breadcrumb a:hover{text-decoration:underline}
.client-card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,0.04)}
.client-card .contact-select-wrapper{position:relative}
.client-card .contact-select-wrapper::after{content:'▼';position:absolute;right:12px;bottom:12px;font-size:10px;color:#64748b;pointer-events:none}
.invoice-header-card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:20px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,0.04)}
.invoice-header-card .grid-2{gap:12px}
.invoice-header-card label{font-size:10px;letter-spacing:0.8px;color:#64748b}
.invoice-header-card input,.invoice-header-card select{padding:8px 12px;font-size:12px;border:1.5px solid #e2e8f0;background:#fff}
.status-badge{display:inline-block;padding:4px 12px;border-radius:20px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px}
.status-brouillon{background:#f1f5f9;color:#475569}
.status-valide{background:#dcfce7;color:#166534}
.status-a_comptabiliser{background:#dbeafe;color:#1e40af}
.line-empty{text-align:center;padding:40px 20px;color:#94a3b8;font-size:13px}
.line-empty .icon{font-size:40px;margin-bottom:10px;opacity:0.4}
.product-search-wrapper{position:relative}
.product-search-results{position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #e2e8f0;border-radius:8px;margin-top:4px;max-height:200px;overflow-y:auto;z-index:50;box-shadow:0 4px 12px rgba(0,0,0,0.1)}
.product-search-item{padding:10px 12px;cursor:pointer;border-bottom:1px solid #f1f5f9;font-size:13px}
.product-search-item:hover{background:#f8fafc}
.product-search-item .ref{color:#4f46e5;font-weight:600;font-size:11px}
.product-search-item .price{color:#10b981;font-weight:600;font-size:12px;margin-left:auto}"""

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


def _page(body):
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
    csrf_js = '<script>window.CSRF_TOKEN=' + _esc(_get_csrf_token()) + ';</script>'
    return "<!DOCTYPE html><html lang='fr'><head><meta charset='utf-8'>" \
           "<meta name='viewport' content='width=device-width,initial-scale=1'>" \
           "<meta name='referrer' content='no-referrer'>" \
           "<title>T-CONNECTOR &middot; {title}</title><style>".format(title=_esc(page_label)) \
           + CSS + "</style></head><body>" \
           + _sidebar(request.path) + '<div class="main-content">' + topbar \
           + csrf_js + body + FOOTER


LOGIN_HTML = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>T-CONNECTOR - Connexion</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:#f4f5fa;color:#1e293b;position:relative;overflow:hidden;padding:24px;
}
body::before{
  content:'';position:fixed;inset:-10%;z-index:0;pointer-events:none;
  background:
    radial-gradient(560px 420px at 12% 8%,rgba(79,70,229,.16),transparent 60%),
    radial-gradient(620px 460px at 92% 92%,rgba(124,58,237,.14),transparent 60%),
    radial-gradient(400px 340px at 85% 12%,rgba(16,185,129,.08),transparent 60%);
}
body::after{
  content:'';position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.5;
  background-image:radial-gradient(rgba(15,23,42,.06) 1px,transparent 1px);
  background-size:24px 24px;
  -webkit-mask-image:radial-gradient(ellipse 70% 60% at 50% 40%,#000 40%,transparent 78%);
  mask-image:radial-gradient(ellipse 70% 60% at 50% 40%,#000 40%,transparent 78%);
}
.auth-wrap{position:relative;z-index:1;width:420px;max-width:100%}
.brand-row{display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:22px}
.brand-row .ico{
  width:32px;height:32px;border-radius:9px;flex-shrink:0;
  background:linear-gradient(135deg,#4f46e5,#7c3aed);
  display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:13px;
  box-shadow:0 4px 12px rgba(79,70,229,.3);
}
.brand-row span{font-size:14px;font-weight:700;color:#1e293b;letter-spacing:.2px}
.card{
  width:100%;
  background:#fff;border:1px solid #e6e9f2;border-radius:20px;
  padding:40px 36px;box-shadow:0 1px 2px rgba(15,23,42,.04),0 30px 60px -30px rgba(15,23,42,.28);
}
.logo{
  width:56px;height:56px;border-radius:15px;margin:0 auto 18px;
  background:linear-gradient(135deg,#4f46e5,#7c3aed);
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 10px 26px -6px rgba(79,70,229,.4);
}
.logo svg{width:30px;height:30px}
h1{
  text-align:center;font-weight:800;color:#0f172a;font-size:22px;
  margin-bottom:4px;letter-spacing:-0.5px;
}
.sub{text-align:center;font-size:13px;color:#64748b;margin-bottom:28px}
.field{margin-bottom:18px}
.field label{
  display:block;font-size:11px;font-weight:700;color:#475569;
  margin-bottom:8px;text-transform:uppercase;letter-spacing:0.7px;
}
.field-wrap{position:relative}
.field-wrap svg{position:absolute;left:14px;top:50%;transform:translateY(-50%);width:16px;height:16px;color:#94a3b8;pointer-events:none}
.field input{
  width:100%;padding:12px 14px 12px 40px;font-size:14px;
  background:#fff;color:#1e293b;
  border:1.5px solid #e2e8f0;border-radius:11px;
  font-family:inherit;transition:all 0.2s;
}
.field input:focus{
  outline:none;border-color:#4f46e5;
  box-shadow:0 0 0 4px rgba(79,70,229,0.12);
}
.field input::placeholder{color:#94a3b8}
.btn{
  width:100%;padding:13px;margin-top:8px;
  background:linear-gradient(135deg,#4f46e5,#7c3aed);
  color:#fff;border:none;border-radius:11px;
  font-size:15px;font-weight:700;cursor:pointer;
  font-family:inherit;letter-spacing:0.3px;
  box-shadow:0 8px 20px -6px rgba(79,70,229,.45);
  transition:all 0.2s;display:flex;align-items:center;justify-content:center;gap:8px;
}
.btn:hover{
  box-shadow:0 12px 28px -6px rgba(79,70,229,.55);
  transform:translateY(-1px);
}
.btn:active{transform:translateY(0)}
.btn svg{width:16px;height:16px}
.error{
  display:flex;align-items:center;gap:8px;
  background:#fef2f2;border:1px solid #fecaca;color:#991b1b;
  font-size:12.5px;font-weight:600;
  padding:11px 13px;border-radius:11px;margin-bottom:18px;
}
.features{
  display:flex;justify-content:space-between;gap:8px;
  margin-top:26px;padding-top:20px;
  border-top:1px solid #eef0f6;
}
.feat{text-align:center;flex:1;min-width:0}
.feat .ico{
  width:32px;height:32px;border-radius:9px;margin:0 auto 6px;
  display:flex;align-items:center;justify-content:center;
}
.feat .ico svg{width:15px;height:15px}
.feat span{font-size:9.5px;color:#64748b;font-weight:600;display:block;line-height:1.3}
.foot{text-align:center;margin-top:22px;font-size:11px;color:#94a3b8;position:relative;z-index:1}
</style></head><body>
<div class="auth-wrap">
<div class="brand-row">
  <div class="ico">TC</div>
  <span>T-CONNECTOR SFEC</span>
</div>
<div class="card">
  <div class="logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
    </svg>
  </div>
  <h1>Bon retour</h1>
  <p class="sub">Connectez-vous a votre espace T-CONNECTOR</p>

  {error}

  <form method="POST" action="/login">
    {csrf}
    <div class="field">
      <label>Adresse email</label>
      <div class="field-wrap">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z" opacity="0"/><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m2 7 10 6 10-6"/></svg>
        <input type="email" name="email" placeholder="admin@example.com" required autofocus>
      </div>
    </div>
    <div class="field">
      <label>Mot de passe</label>
      <div class="field-wrap">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        <input type="password" name="password" placeholder="Entrez votre mot de passe" required>
      </div>
    </div>
    <button type="submit" class="btn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>
      Se connecter
    </button>
  </form>

  <div class="features">
    <div class="feat">
      <div class="ico" style="background:#eef2ff"><svg viewBox="0 0 24 24" fill="none" stroke="#4f46e5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></div>
      <span>Facturation</span>
    </div>
    <div class="feat">
      <div class="ico" style="background:#ecfdf5"><svg viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></div>
      <span>SFEC</span>
    </div>
    <div class="feat">
      <div class="ico" style="background:#f5f3ff"><svg viewBox="0 0 24 24" fill="none" stroke="#7c3aed" stroke-width="2"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg></div>
      <span>Sync Sage</span>
    </div>
    <div class="feat">
      <div class="ico" style="background:#fffbeb"><svg viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg></div>
      <span>POS</span>
    </div>
  </div>
</div>
<p class="foot">T-CONNECTOR SFEC &copy; 2025 &mdash; Sodico</p>
</div>
</body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if not _auth_enabled():
        if request.method == "POST":
            return redirect("/")
        return redirect("/")
    if request.method == "GET":
        if session.get("user_id"):
            return redirect("/")
        token = _get_csrf_token()
        csrf_field = ('<input type="hidden" name="_csrf" value="{}">').format(_esc(token))
        html = LOGIN_HTML.replace("{error}", "").replace("{csrf}", csrf_field)
        return render_template_string(html)

    _apply_session_security()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    if not session.get("_csrf") or not _csrf_valid():
        return render_template_string(
            LOGIN_HTML.replace("{error}", '<div class="error">Session expirée - reessayez</div>')
            .replace("{csrf}", '<input type="hidden" name="_csrf" value="{}">'.format(_esc(_get_csrf_token())))
        ), 400
    user, err = user_auth.authenticate(email, password, ip=request.remote_addr or "")
    if not user:
        return render_template_string(
            LOGIN_HTML.replace("{error}", '<div class="error">{msg}</div>'.format(msg=_esc(err or "Erreur")))
            .replace("{csrf}", '<input type="hidden" name="_csrf" value="{}">'.format(_esc(_get_csrf_token())))
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

    body = """
<div class="card"><h2>Tableau de bord</h2><div class="stat-grid">
<div class="stat"><div class="icon-chip" style="background:#eff6ff;color:#4f46e5"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></div><div class="value">{sales}</div><div class="label">Factures Vente</div></div>
<div class="stat"><div class="icon-chip" style="background:#fef3c7;color:#d97706"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/></svg></div><div class="value">{purchases}</div><div class="label">Factures Achat</div></div>
<div class="stat"><div class="icon-chip" style="background:#ede9fe;color:#7c3aed"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></div><div class="value">{val_count}</div><div class="label">A comptabiliser</div></div>
<div class="stat"><div class="icon-chip" style="background:#dcfce7;color:#16a34a"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg></div><div class="value">{contacts}</div><div class="label">Contacts</div></div>
<div class="stat"><div class="icon-chip" style="background:#dbeafe;color:#2563eb"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg></div><div class="value">{tax}</div><div class="label">Taux TVA</div></div>
<div class="stat"><div class="icon-chip" style="background:#fce7f3;color:#db2777"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg></div><div class="value">{syncs}</div><div class="label">Syncs</div></div>
<div class="stat"><div class="icon-chip" style="background:#ecfdf5;color:#059669"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></div><div class="value">{certs}</div><div class="label">Certifiees SFEC</div></div>
</div></div>
<div class="grid-2"><div class="card"><h2>Etat du systeme</h2><table>
<tr><td>Base de donnees</td><td><span class="badge {db_ok}">{db_txt}</span></td></tr>
<tr><td>Connexion Internet</td><td><span class="badge {net_cls}">{net_txt}</span></td></tr>
<tr><td>Derniere sync</td><td>{last_sync}</td></tr>
<tr><td>Erreur</td><td>{error}</td></tr>
<tr><td>SFEC API</td><td><span class="badge {sfec_ok}">{sfec_txt}</span></td></tr>
<tr><td>URL SFEC</td><td>{sfec_url}</td></tr>
{retry_html}
<tr><td>Polling auto</td><td><span class="badge badge-ok">Actif</span></td></tr>
</table></div>
<div class="card no-print"><h2>Actions</h2>
<button class="btn btn-primary" onclick="syncNow()">Sync Manuelle</button>
<button class="btn btn-warning" onclick="location.href='/pending'" style="margin-left:8px">A certifier</button>
<button class="btn btn-success" onclick="location.href='/certified'" style="margin-left:8px">Certifiees</button>
<div style="margin-top:8px;font-size:12px;color:#64748b" id="sync-status"></div>
</div></div>""".format(
        sales=m.get("sales_invoices", 0), purchases=m.get("purchase_invoices", 0),
        val_count=val_count,
        contacts=m.get("contacts", 0), tax=m.get("tax_rates", 0),
        syncs=m.get("sync_count", 0), certs=m.get("certified_count", 0),
        db_ok=db_ok, db_txt=db_txt, last_sync=_esc(m.get("last_sync_at") or "Jamais"),
        error=_esc(m.get("last_error") or "Aucune"),
        sfec_ok=sfec_ok, sfec_txt=sfec_txt, sfec_url=_esc(sfec.get("url", "")),
        net_cls=net_cls, net_txt=net_txt, retry_html=retry_html
    )
    return _page(body)


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
    body = """
<div class="card"><h2>Factures ({inv_type})</h2>
<div style="margin-bottom:12px" class="no-print">
<a href="/invoices?type=sale" class="btn btn-sm {sale_cls}">Vente</a>
<a href="/invoices?type=purchase" class="btn btn-sm {purchase_cls}">Achat</a>
<button class="btn btn-sm btn-primary" onclick="syncNow()" style="margin-left:8px">Sync</button>
</div>
<table><thead><tr><th>Numero</th><th>Date</th><th>Tiers</th>
<th style="text-align:right">Montant TTC</th><th>Statut</th><th>Valide</th><th>SFEC</th><th>N Certif</th></tr></thead>
<tbody>{rows}{empty}</tbody></table></div>""".format(
        inv_type=inv_type, sale_cls=sale_cls, purchase_cls=purchase_cls,
        rows=rows, empty=empty
    )
    return _page(body)


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
    body = """
<div class="card"><h2>En attente de certification SFEC ({count})</h2>{sfec_warn}
<p style="font-size:13px;color:#94a3b8;margin-bottom:12px">{val_count} facture(s) a comptabiliser (DO_Statut=2)</p>
<table><thead><tr><th>Numero</th><th>Date</th><th>Tiers</th>
<th style="text-align:right">Montant TTC</th><th>Action</th></tr></thead>
<tbody>{rows}{empty}</tbody></table>
<div id="cert-status" style="margin-top:12px"></div></div>
<script>
function certifySingle(id){{
document.getElementById("cert-status").textContent="Certification en cours...";
api("POST","/api/sfec/certify",{{invoice_id:id}}).then(function(d){{
if(d.success){{
showToast("Certifie: "+d.certification_number,"ok");
setTimeout(function(){{ location.reload(); }}, 1500);
}} else {{
showToast("Erreur: "+d.error,"err");
if(d.queued){{ document.getElementById("cert-status").innerHTML='<span class="queue-badge">Mis en file d attente (offline)</span>'; }}
}}
}});
}}
function toMonitor(id){{
document.getElementById("cert-status").textContent="Marquage a surveiller...";
api("POST","/api/sfec/to-monitor",{{invoice_id:id}}).then(function(d){{
if(d.success){{
showToast("Marque a surveiller","warn");
setTimeout(function(){{ location.reload(); }}, 1500);
}} else {{
showToast("Erreur: "+d.error,"err");
}}
}});
}}
</script>""".format(
        count=len(uncertified),
        sfec_warn=sfec_warn, val_count=val_count, rows=rows, empty=empty
    )
    return _page(body)


@app.route("/certified")
@_login_required
def certified_page():
    cache = get_cache()
    last_sfec = cache.get("last_sfec_sync_at", "")
    body = r"""
<div class="card"><h2>Factures certifiees SFEC</h2>
<div class="stat-grid" style="margin-bottom:16px">
<div class="stat"><div class="value" id="c-tot">-</div><div class="label">Factures SFEC</div></div>
<div class="stat"><div class="value" id="c-aff">-</div><div class="label">Affichées</div></div>
</div>
<button class="btn btn-sm btn-success no-print" id="sfec-refresh" onclick="refreshCert()">Sync SFEC</button>
<span id="sfec-status" style="margin-left:8px;font-size:12px;color:#64748b">Derniere sync: @LAST@</span>
<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:14px">
<input id="c-search" placeholder="N° facture, client..." style="min-width:240px;padding:8px 10px;border:1px solid #334155;border-radius:8px;background:#0f172a;color:#e2e8f0">
<select id="c-statut" style="height:38px;padding:0 8px;border:1px solid #334155;border-radius:8px;background:#0f172a;color:#e2e8f0"><option value="">Tous statuts</option></select>
<input type="date" id="c-dfrom" title="Date certif depuis" style="height:38px;padding:0 8px;border:1px solid #334155;border-radius:8px;background:#0f172a;color:#e2e8f0;color-scheme:dark">
<span style="color:#94a3b8">→</span>
<input type="date" id="c-dto" title="Date certif jusqu'a" style="height:38px;padding:0 8px;border:1px solid #334155;border-radius:8px;background:#0f172a;color:#e2e8f0;color-scheme:dark">
<button class="btn btn-sm no-print" onclick="resetCerts()">Reinitialiser</button>
</div>
</div>
<div class="card"><h2>Liste des certifications</h2>
<table><thead>
<tr>
<th data-sort="numero" onclick="toggleSortC('numero')">Numero<span id="so_numero"></span></th>
<th data-sort="date" onclick="toggleSortC('date')">Date certif<span id="so_date"></span></th>
<th data-sort="statut" onclick="toggleSortC('statut')">Statut<span id="so_statut"></span></th>
<th data-sort="buyer" onclick="toggleSortC('buyer')">Client<span id="so_buyer"></span></th>
<th data-sort="montant" onclick="toggleSortC('montant')" style="text-align:right">Montant TTC<span id="so_montant"></span></th>
<th class="no-print">Action</th>
</tr></thead>
<tbody id="cert-body"><tr><td colspan="6" style="text-align:center;color:#64748b">Chargement...</td></tr></tbody>
</table>
<div class="no-print" id="cert-pag" style="margin-top:12px"></div>
</div>
<script>
var bsC={list:[],q:"",statut:"",dfrom:"",dto:"",sort:"date",dir:-1,page:1,limit:25,per:[25,50,100]};
function normFr(v){return String(v==null?"":v).toLowerCase().replace(/[àâäã]/g,"a").replace(/[éèêë]/g,"e").replace(/[îï]/g,"i").replace(/[ôö]/g,"o").replace(/[ûüù]/g,"u").replace(/ç/g,"c");}
function escC(v){return String(v==null?"":v).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function fmtMoney(v){var x=parseFloat(v);if(isNaN(x))return String(v==null?"":v);return x.toLocaleString("fr-FR",{maximumFractionDigits:0});}
function badgeStatut(st){
  st=(st||"").trim();
  if(!st)return '<span class="badge badge-info">-</span>';
  var l=normFr(st);
  if(/certif/.test(l)||l==="ok"||l==="valide")return '<span class="badge badge-ok">'+escC(st)+'</span>';
  if(/pend|attent|brouillon|en cours/.test(l))return '<span class="badge badge-warn">'+escC(st)+'</span>';
  if(/fail|err|rejet|refus|echec/.test(l))return '<span class="badge badge-err">'+escC(st)+'</span>';
  return '<span class="badge badge-info">'+escC(st)+'</span>';
}
function filteredCert(){
  var qs=bsC.q?normFr(bsC.q).split(/\s+/).filter(Boolean):[];
  var r=bsC.list.filter(function(v){
    var hay=normFr(v.numero+" "+v.buyer);
    for(var i=0;i<qs.length;i++){if(hay.indexOf(qs[i])<0)return false;}
    if(bsC.statut&&normFr(v.statut)!==bsC.statut)return false;
    if(bsC.dfrom&&(v.date||"")<bsC.dfrom)return false;
    if(bsC.dto&&(v.date||"")>bsC.dto)return false;
    return true;
  });
  var ks={
    numero:function(a,b){return a.numero.localeCompare(b.numero)},
    date:function(a,b){return (a.date||"").localeCompare(b.date||"")},
    statut:function(a,b){return normFr(a.statut).localeCompare(normFr(b.statut))},
    buyer:function(a,b){return normFr(a.buyer).localeCompare(normFr(b.buyer))},
    montant:function(a,b){return (parseFloat(a.montant)||0)-(parseFloat(b.montant)||0)}
  };
  r.sort(ks[bsC.sort]||ks.date);
  if(bsC.dir<0)r.reverse();
  return r;
}
function renderCert(){
  var rows=filteredCert();
  document.getElementById("c-tot").textContent=bsC.list.length;
  document.getElementById("c-aff").textContent=rows.length;
  var maxPage=Math.max(1,Math.ceil(rows.length/bsC.limit));
  if(bsC.page>maxPage)bsC.page=maxPage;
  var start=(bsC.page-1)*bsC.limit;
  var slice=rows.slice(start,start+bsC.limit);
  var html="";
  for(var i=0;i<slice.length;i++){
    var v=slice[i];
    html+="<tr><td>"+escC(v.numero)+"</td><td>"+escC(v.date||"-")+"</td><td>"+badgeStatut(v.statut)+"</td><td>"+escC(v.buyer||"-")+"</td><td style='text-align:right'>"+fmtMoney(v.montant)+"</td><td class='no-print'><a class='btn btn-sm btn-primary' href='/certified/"+encodeURIComponent(v.id)+"/print' target='_blank'>Imprimer</a></td></tr>";
  }
  if(!slice.length)html='<tr><td colspan="6" style="text-align:center;color:#64748b">Aucune facture correspondante</td></tr>';
  document.getElementById("cert-body").innerHTML=html;
  setArrowsC();
  pagC();
}
function setArrowsC(){
  ["numero","date","statut","buyer","montant"].forEach(function(k){
    var el=document.getElementById("so_"+k);
    if(el)el.textContent=(k===bsC.sort?(bsC.dir>0?"▲":"▼"):"");
  });
}
function toggleSortC(k){if(bsC.sort===k){bsC.dir=-bsC.dir;}else{bsC.sort=k;bsC.dir=1;}bsC.page=1;renderCert();}
function pagC(){
  var rows=filteredCert();
  var maxPage=Math.max(1,Math.ceil(rows.length/bsC.limit));
  var html='<span style="color:#94a3b8;margin-right:8px">'+rows.length+' facture(s)</span>';
  html+='<select onchange="setLimitC(this.value)" style="height:32px;padding:0 8px;border:1px solid #334155;border-radius:8px;background:#0f172a;color:#e2e8f0">';
  for(var i=0;i<bsC.per.length;i++){html+='<option value="'+bsC.per[i]+'"'+(bsC.limit===bsC.per[i]?' selected':'')+'>'+bsC.per[i]+'</option>';}
  html+='</select>';
  html+='<button class="btn btn-sm" onclick="pgC('+(bsC.page-1)+')"'+(bsC.page<=1?' disabled':'')+'>Prec</button>';
  var jumped=false;
  for(var p=1;p<=maxPage;p++){
    if(maxPage>9&&p!==1&&p!==maxPage&&Math.abs(p-bsC.page)>2){
      if(!jumped){html+='<span style="color:#64748b">…</span>';jumped=true;}
      continue;
    }
    jumped=false;
    html+='<button class="btn btn-sm'+(p===bsC.page?' btn-primary':'')+'" onclick="pgC('+p+')">'+p+'</button>';
  }
  html+='<button class="btn btn-sm" onclick="pgC('+(bsC.page+1)+')"'+(bsC.page>=maxPage?' disabled':'')+'>Suiv</button>';
  document.getElementById("cert-pag").innerHTML=html;
}
function pgC(p){var maxPage=Math.max(1,Math.ceil(filteredCert().length/bsC.limit));if(p<1)p=1;if(p>maxPage)p=maxPage;bsC.page=p;renderCert();}
function setLimitC(v){bsC.limit=parseInt(v,10);bsC.page=1;renderCert();}
function resetCerts(){bsC.q="";bsC.statut="";bsC.dfrom="";bsC.dto="";bsC.page=1;
  document.getElementById("c-search").value="";
  document.getElementById("c-statut").value="";
  document.getElementById("c-dfrom").value="";
  document.getElementById("c-dto").value="";
  renderCert();
}
function refreshCert(){
  var st=document.getElementById("sfec-status");
  var bt=document.getElementById("sfec-refresh");
  st.textContent="Chargement en cours...";
  bt.disabled=true;
  api("POST","/api/sfec/sync",{}).then(function(){setTimeout(loadCerts,2500);})
    .catch(function(e){st.textContent="Erreur: "+e;bt.disabled=false;});
}
function loadCerts(){
  api("GET","/api/certified/list").then(function(d){
    bsC.list=d.invoices||[];
    var statuts={};
    bsC.list.forEach(function(v){if(v.statut)statuts[normFr(v.statut)]=v.statut;});
    var keys=Object.keys(statuts).sort();
    var opts='<option value="">Tous statuts</option>';
    for(var i=0;i<keys.length;i++){opts+='<option value="'+normFr(statuts[keys[i]])+'">'+escC(statuts[keys[i]])+'</option>';}
    document.getElementById("c-statut").innerHTML=opts;
    if(d.last_sfec)document.getElementById("sfec-status").textContent="Derniere sync: "+d.last_sfec.slice(0,19).replace("T"," ");
    document.getElementById("sfec-refresh").disabled=false;
    renderCert();
  }).catch(function(e){
    document.getElementById("cert-body").innerHTML='<tr><td colspan="6" style="text-align:center;color:#64748b">Erreur de chargement: '+e+'</td></tr>';
    document.getElementById("c-tot").textContent="0";
    document.getElementById("sfec-refresh").disabled=false;
  });
}
document.getElementById("c-search").addEventListener("input",function(){bsC.q=this.value;bsC.page=1;renderCert();});
document.getElementById("c-statut").addEventListener("change",function(){bsC.statut=normFr(this.value);bsC.page=1;renderCert();});
document.getElementById("c-dfrom").addEventListener("change",function(){bsC.dfrom=this.value;bsC.page=1;renderCert();});
document.getElementById("c-dto").addEventListener("change",function(){bsC.dto=this.value;bsC.page=1;renderCert();});
loadCerts();
</script>"""
    out = body.replace("@LAST@", (last_sfec or "")[:19].replace("T", " ") or "jamais")
    return _page(out)


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

    body = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>Facture {invoice_number} - SFEC</title>
<style>
body{{font-family:'Segoe UI',sans-serif;padding:40px;color:#000;background:#fff}}
.header{{display:flex;justify-content:space-between;margin-bottom:30px;border-bottom:2px solid #333;padding-bottom:20px}}
.company-name{{font-size:24px;font-weight:bold}}
.company-info{{font-size:12px;color:#666}}
.cert-box{{background:#f0f0f0;border:2px solid #333;padding:16px;margin:20px 0;border-radius:4px}}
.cert-box h3{{margin-bottom:8px;font-size:14px}}
table{{width:100%;border-collapse:collapse;margin:16px 0}}
th,td{{border:1px solid #ccc;padding:8px;text-align:left;font-size:13px}}
th{{background:#f5f5f5}}
.total-row td{{border-top:2px solid #333;font-weight:bold;font-size:14px}}
.qr-section{{text-align:center;margin:30px 0;padding:20px;border:1px dashed #999}}
.footer{{margin-top:40px;font-size:11px;color:#666;border-top:1px solid #ccc;padding-top:10px}}
@media print{{.no-print{{display:none!important}}}}
</style></head><body>
<div class="header">
<div>
<div class="company-name">{seller_name}</div>
<div class="company-info">{seller_addr}</div>
<div class="company-info">NIU: {seller_niu}</div>
<div class="company-info">RCCM: {seller_rccm}</div>
</div>
<div style="text-align:right">
<h2>FACTURE</h2>
<div class="company-info">Numero: {invoice_number}</div>
<div class="company-info">Date: {invoice_date}</div>
<div class="company-info">Devise: {currency}</div>
</div>
</div>

<div style="display:flex;justify-content:space-between;margin-bottom:20px">
<div style="border:1px solid #ccc;padding:12px;border-radius:4px;flex:1;margin-right:8px">
<h3 style="font-size:13px;margin-bottom:8px">Client</h3>
<div><strong>{buyer_name}</strong></div>
<div class="company-info">NIU: {buyer_niu}</div>
<div class="company-info">{buyer_addr}</div>
<div class="company-info">{buyer_phone}</div>
</div>
<div style="border:1px solid #ccc;padding:12px;border-radius:4px;flex:1;margin-left:8px">
<h3 style="font-size:13px;margin-bottom:8px">Paiement</h3>
<div>Methode: {payment_method}</div>
<div>Montant du: <strong>{amount_due} {currency}</strong></div>
</div>
</div>

<table>
<thead><tr><th>Designation</th><th style="text-align:right">Qte</th><th style="text-align:right">Prix unit.</th><th style="text-align:right">TVA</th><th style="text-align:right">Montant HT</th></tr></thead>
<tbody>{item_rows}
<tr class="total-row"><td colspan="4">Total HT</td><td style="text-align:right">{total_ht} {currency}</td></tr>
<tr class="total-row"><td colspan="4">TVA 18%</td><td style="text-align:right">{total_tax18} {currency}</td></tr>
<tr class="total-row"><td colspan="4">TVA 5%</td><td style="text-align:right">{total_tax5} {currency}</td></tr>
<tr class="total-row"><td colspan="4"><strong>TOTAL TTC</strong></td><td style="text-align:right"><strong>{total_ttc} {currency}</strong></td></tr>
</tbody></table>

<div class="cert-box">
<h3>SFEC - Systeme de Facturation Electronique Certifie</h3>
<table>
<tr><td><strong>Statut certification</strong></td><td>{cert_status}</td></tr>
<tr><td><strong>Signature courte</strong></td><td>{short_sig}</td></tr>
<tr><td><strong>Signature complete</strong></td><td style="word-break:break-all;font-size:10px">{signature}</td></tr>
<tr><td><strong>Date de certification</strong></td><td>{cert_date}</td></tr>
</table>
</div>

<div class="qr-section">
<h3>QR Code de certification</h3>
{qr_display}
</div>

<div class="footer no-print">
<button class="btn btn-primary" onclick="window.print()">Imprimer</button>
<a href="/certified" style="margin-left:8px;color:#38bdf8">Retour</a>
</div>
</body></html>""".format(
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
    return body


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

    body = """<div class="card"><h2>Configuration</h2>
<div class="tabs">
<button class="tab-btn active" id="tab-btn-db" onclick="switchTab('db')">Base de Donnees</button>
<button class="tab-btn" id="tab-btn-sfec" onclick="switchTab('sfec')">API SFEC</button>
<button class="tab-btn" id="tab-btn-company" onclick="switchTab('company')">Entreprise</button>
<button class="tab-btn" id="tab-btn-sync" onclick="switchTab('sync')">Synchronisation</button>
<button class="tab-btn" id="tab-btn-validation" onclick="switchTab('validation')">Validation & Limites</button>
<button class="tab-btn" id="tab-btn-notif" onclick="switchTab('notif')">Notifications</button>
<button class="tab-btn" id="tab-btn-system" onclick="switchTab('system')">Systeme</button>
</div>"""

    # ── TAB 1: DATABASE ──
    body += """
<div class="tab-content active" id="tab-db">
<h2 style="margin-bottom:16px">Base de donnees Sage 100</h2>
<form id="form-db" onsubmit="event.preventDefault();saveForm('form-db','/api/config/db')">
<div class="grid-2">
<div><label>Serveur</label><input type="text" name="server" value="{server}"></div>
<div><label>Port</label><input type="number" name="port" value="{port}"></div>
<div><label>Base de donnees</label><input type="text" name="database" value="{database}"><div class="desc">Nom exact de la base Sage 100</div></div>
<div><label>Utilisateur</label><input type="text" name="user" value="{user}"></div>
<div><label>Mot de passe</label><input type="password" name="password" value="{password}"></div>
<div><label>Connexion Windows</label><select name="trusted_connection"><option value="false" {tc_f}>Non</option><option value="true" {tc_t}>Oui</option></select></div>
<div><label>Chiffrer</label><select name="encrypt"><option value="false" {enc_f}>Non</option><option value="true" {enc_t}>Oui</option></select></div>
<div><label>Confiance certificat</label><select name="trust_server_certificate"><option value="true" {tsc_t}>Oui</option><option value="false" {tsc_f}>Non</option></select></div>
</div>
<h2 style="margin-top:16px">Pool & Timeouts</h2>
<div class="grid-2">
<div><label>Connexions max</label><input type="number" name="pool_max" value="{pool_max}"></div>
<div><label>Connexions min</label><input type="number" name="pool_min" value="{pool_min}"></div>
<div><label>Idle timeout (ms)</label><input type="number" name="pool_idle_timeout_ms" value="{pool_idle_timeout_ms}"></div>
<div><label>Connection timeout (ms)</label><input type="number" name="connection_timeout_ms" value="{connection_timeout_ms}"></div>
<div><label>Request timeout (ms)</label><input type="number" name="request_timeout_ms" value="{request_timeout_ms}"></div>
</div>
<div style="margin-top:16px">
<button type="submit" class="btn btn-primary">Sauvegarder</button>
<button type="button" class="btn btn-success" onclick="testDb()">Tester la connexion</button>
<div id="db-test" style="margin-top:8px"></div>
</div>
</form></div>""".format(
        server=_esc(db.get("server", "")), port=_esc(db.get("port", 1433)), database=_esc(db.get("database", "")),
        user=_esc(db.get("user", "")), password=_esc(db.get("password", "")),
        tc_f=_s(db.get("trusted_connection"), False), tc_t=_s(db.get("trusted_connection"), True),
        enc_f=_s(db.get("encrypt"), False), enc_t=_s(db.get("encrypt"), True),
        tsc_t=_s(db.get("trust_server_certificate"), True), tsc_f=_s(db.get("trust_server_certificate"), False),
        pool_max=db.get("pool_max", 10), pool_min=db.get("pool_min", 2),
        pool_idle_timeout_ms=db.get("pool_idle_timeout_ms", 30000),
        connection_timeout_ms=db.get("connection_timeout_ms", 15000),
        request_timeout_ms=db.get("request_timeout_ms", 30000),
    )

    # ── TAB 2: SFEC ──
    body += """
<div class="tab-content" id="tab-sfec">
<h2 style="margin-bottom:16px">Configuration API SFEC</h2>
<form id="form-sfec" onsubmit="event.preventDefault();saveForm('form-sfec','/api/config/sfec')">
<div class="grid-2">
<div><label>Cle API SFEC</label><input type="text" name="api_key" value="{api_key}"></div>
<div><label>Environnement</label><select name="use_sandbox"><option value="true" {sb_t}>Sandbox (test)</option><option value="false" {sb_f}>Production</option></select></div>
<div><label>SFEC active</label><select name="enabled"><option value="true" {en_t}>Oui</option><option value="false" {en_f}>Non</option></select></div>
<div><label>Statut pour certification</label><select name="certify_status">
<option value="SAISI" {cs_sa}>SAISI</option>
<option value="CONFIRME" {cs_co}>CONFIRME</option>
<option value="A COMPTABILISER" {cs_ac}>A COMPTABILISER</option>
<option value="COMPTABILISE" {cs_c}>COMPTABILISE</option>
<option value="LETTRÉ" {cs_l}>LETTRÉ</option></select></div>
<div><label>URL Production</label><input type="text" name="base_url" value="{base_url}"></div>
<div><label>URL Sandbox</label><input type="text" name="sandbox_url" value="{sandbox_url}"></div>
</div>
<div style="margin-top:16px">
<button type="submit" class="btn btn-primary">Sauvegarder</button>
<button type="button" class="btn btn-success" onclick="testSfec()">Tester la connexion SFEC</button>
<div id="sfec-test" style="margin-top:8px"></div>
</div>
</form></div>""".format(
        api_key=_esc(sfec.get("api_key", "")),
        sb_t=_s(sfec.get("use_sandbox"), True), sb_f=_s(sfec.get("use_sandbox"), False),
        en_t=_s(sfec.get("enabled"), True), en_f=_s(sfec.get("enabled"), False),
        cs_sa=_s(sfec.get("certify_status"), "SAISI"),
        cs_co=_s(sfec.get("certify_status"), "CONFIRME"),
        cs_ac=_s(sfec.get("certify_status"), "A COMPTABILISER"),
        cs_c=_s(sfec.get("certify_status"), "COMPTABILISE"),
        cs_l=_s(sfec.get("certify_status"), "LETTRÉ"),
        base_url=_esc(sfec.get("base_url", "")), sandbox_url=_esc(sfec.get("sandbox_url", "")),
    )

    # ── TAB 3: COMPANY ──
    body += """
<div class="tab-content" id="tab-company">
<h2 style="margin-bottom:16px">Configuration Entreprise</h2>
<form id="form-company" onsubmit="event.preventDefault();saveForm('form-company','/api/config/company')">
<div class="grid-2">
<div><label>Nom de l'entreprise</label><input type="text" name="name" value="{name}"></div>
<div><label>NIU (Tax Number)</label><input type="text" name="tax_number" value="{tax_number}"></div>
<div><label>Adresse</label><input type="text" name="address" value="{address}"></div>
<div><label>Ville</label><input type="text" name="city" value="{city}"></div>
<div><label>Pays</label><input type="text" name="country" value="{country}"></div>
<div><label>Telephone</label><input type="text" name="phone" value="{phone}"></div>
<div><label>Email</label><input type="email" name="email" value="{email}"></div>
<div><label>Devise</label><select name="currency"><option value="XAF" {cur_xaf}>XAF (FCFA)</option><option value="USD" {cur_usd}>USD</option></select></div>
</div>
<h2 style="margin-top:16px">Banque</h2>
<div class="grid-2">
<div><label>Banque</label><input type="text" name="bank_name" value="{bank_name}"></div>
<div><label>Compte</label><input type="text" name="bank_account" value="{bank_account}"></div>
<div><label>IBAN</label><input type="text" name="bank_iban" value="{bank_iban}"></div>
<div><label>SWIFT</label><input type="text" name="bank_swift" value="{bank_swift}"></div>
</div>
<h2 style="margin-top:16px">Facturation</h2>
<div class="grid-2">
<div><label>Prefixe facture</label><input type="text" name="invoice_prefix" value="{invoice_prefix}"></div>
<div><label>Numero suivant</label><input type="number" name="invoice_next_number" value="{invoice_next_number}"></div>
<div><label>Notes facture</label><input type="text" name="invoice_notes" value="{invoice_notes}"></div>
<div><label>Pied de page</label><input type="text" name="invoice_footer" value="{invoice_footer}"></div>
<div><label>Regime fiscal</label><input type="text" name="tax_regime" value="{tax_regime}"></div>
<div><label>QR Code active</label><select name="qrcode_enabled"><option value="true" {qr_t}>Oui</option><option value="false" {qr_f}>Non</option></select></div>
</div>
<div style="margin-top:16px"><button type="submit" class="btn btn-primary">Sauvegarder</button></div>
</form></div>""".format(
        name=_esc(company.get("name", "")), tax_number=_esc(company.get("tax_number", "")),
        address=_esc(company.get("address", "")), city=_esc(company.get("city", "")),
        country=_esc(company.get("country", "")), phone=_esc(company.get("phone", "")),
        email=_esc(company.get("email", "")),
        cur_xaf=_s(company.get("currency"), "XAF"), cur_usd=_s(company.get("currency"), "USD"),
        bank_name=_esc(company.get("bank_name", "")), bank_account=_esc(company.get("bank_account", "")),
        bank_iban=_esc(company.get("bank_iban", "")), bank_swift=_esc(company.get("bank_swift", "")),
        invoice_prefix=_esc(company.get("invoice_prefix", "INV-")),
        invoice_next_number=_esc(company.get("invoice_next_number", 1)),
        invoice_notes=_esc(company.get("invoice_notes", "")),
        invoice_footer=_esc(company.get("invoice_footer", "")),
        tax_regime=_esc(company.get("tax_regime", "")),
        qr_t=_s(company.get("qrcode_enabled"), True), qr_f=_s(company.get("qrcode_enabled"), False),
    )

    # ── TAB 4: SYNC ──
    body += """
<div class="tab-content" id="tab-sync">
<h2 style="margin-bottom:16px">Synchronisation & Polling</h2>
<form id="form-sync" onsubmit="event.preventDefault();saveForm('form-sync','/api/config/sync')">
<div class="grid-2">
<div><label>Polling active</label><select name="polling_enabled"><option value="true" {pe_t}>Oui</option><option value="false" {pe_f}>Non</option></select><div class="desc">Active/desactive la synchro automatique</div></div>
<div><label>Intervalle polling (ms)</label><input type="number" name="polling_interval_ms" value="{poll_ms}"><div class="desc">Recommande: 30000 (30 secondes)</div></div>
</div>
<h2 style="margin-top:16px">File d'attente</h2>
<div class="grid-2">
<div><label>Concurrence max</label><input type="number" name="max_concurrent" value="{max_concurrent}"><div class="desc">Certifications SFEC simultanees</div></div>
<div><label>Retries max</label><input type="number" name="max_retries" value="{max_retries}"><div class="desc">Nombre de tentatives en cas d'echec</div></div>
<div><label>Delai retry (ms)</label><input type="number" name="retry_base_delay_ms" value="{retry_base_delay_ms}"><div class="desc">Delai de base entre les retries</div></div>
<div><label>Taille max queue</label><input type="number" name="max_queue_size" value="{max_queue_size}"></div>
<div><label>Timeout job (ms)</label><input type="number" name="job_timeout_ms" value="{job_timeout_ms}"></div>
</div>
<div style="margin-top:16px"><button type="submit" class="btn btn-primary">Sauvegarder</button></div>
</form></div>""".format(
        pe_t=_s(db.get("polling_enabled"), True), pe_f=_s(db.get("polling_enabled"), False),
        poll_ms=db.get("polling_interval_ms", 30000),
        max_concurrent=queue.get("max_concurrent", 5),
        max_retries=queue.get("max_retries", 4),
        retry_base_delay_ms=queue.get("retry_base_delay_ms", 2000),
        max_queue_size=queue.get("max_queue_size", 10000),
        job_timeout_ms=queue.get("job_timeout_ms", 60000),
    )

    # ── TAB 5: VALIDATION & LIMITES ──
    notif_events = notifications.get("events", [])
    body += """
<div class="tab-content" id="tab-validation">
<h2 style="margin-bottom:16px">Validation</h2>
<form id="form-validation" onsubmit="event.preventDefault();saveForm('form-validation','/api/config/validation')">
<div class="grid-2">
<div><label>Mode strict</label><select name="strict_mode"><option value="true" {st_t}>Oui</option><option value="false" {st_f}>Non</option></select><div class="desc">Verification stricte des donnees avant certification</div></div>
<div><label>Tolerance montant</label><input type="number" name="tolerance_amount" value="{tolerance}" step="0.01"><div class="desc">Ecart autorise entre les totaux</div></div>
</div>
<h2 style="margin-top:16px">Limites API SFEC</h2>
<div class="grid-2">
<div><label>Maximum journalier</label><input type="number" name="daily_max" value="{daily_max}"><div class="desc">Nombre max de certifications par jour</div></div>
<div><label>Maximum par minute</label><input type="number" name="per_minute_max" value="{per_minute_max}"></div>
<div><label>Seuil d'alerte (%)</label><input type="number" name="alert_threshold_percent" value="{alert_threshold}"><div class="desc">Declenche une alerte a ce %</div></div>
</div>
<div style="margin-top:16px"><button type="submit" class="btn btn-primary">Sauvegarder</button></div>
</form></div>""".format(
        st_t=_s(validation.get("strict_mode"), True), st_f=_s(validation.get("strict_mode"), False),
        tolerance=validation.get("tolerance_amount", 0.01),
        daily_max=rate_limit.get("daily_max", 2500),
        per_minute_max=rate_limit.get("per_minute_max", 100),
        alert_threshold=rate_limit.get("alert_threshold_percent", 80),
    )

    # ── TAB 6: NOTIFICATIONS ──
    body += """
<div class="tab-content" id="tab-notif">
<h2 style="margin-bottom:16px">Notifications</h2>
<form id="form-notif" onsubmit="event.preventDefault();saveForm('form-notif','/api/config/notifications')">
<div class="grid-2">
<div><label>Notifications activees</label><select name="enabled"><option value="true" {en_t}>Oui</option><option value="false" {en_f}>Non</option></select></div>
</div>
<h2 style="margin-top:16px">Evenements</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
<div><input type="checkbox" name="evt_synced" value="invoice:synced" {ev_synced}> Facture synchro</div>
<div><input type="checkbox" name="evt_val_error" value="invoice:validation_error" {ev_val}> Erreur validation</div>
<div><input type="checkbox" name="evt_dlq" value="queue:dlq_added" {ev_dlq}> File d'attente</div>
<div><input type="checkbox" name="evt_db_fail" value="db:connection_failed" {ev_dbf}> Echec connexion DB</div>
<div><input type="checkbox" name="evt_db_err" value="db:polling_error" {ev_dbe}> Erreur polling</div>
</div>
<div style="margin-top:16px"><button type="submit" class="btn btn-primary">Sauvegarder</button></div>
</form></div>""".format(
        en_t=_s(notifications.get("enabled"), True), en_f=_s(notifications.get("enabled"), False),
        ev_synced=_ck("invoice:synced" in notif_events),
        ev_val=_ck("invoice:validation_error" in notif_events),
        ev_dlq=_ck("queue:dlq_added" in notif_events),
        ev_dbf=_ck("db:connection_failed" in notif_events),
        ev_dbe=_ck("db:polling_error" in notif_events),
    )

    # ── TAB 7: SYSTEM ──
    body += """
<div class="tab-content" id="tab-system">
<h2 style="margin-bottom:16px">Systeme & Dashboard</h2>
<form id="form-system" onsubmit="event.preventDefault();saveForm('form-system','/api/config/system')">
<div class="grid-2">
<div><label>Niveau de log</label><select name="log_level">
<option value="debug" {ll_d}>Debug</option>
<option value="info" {ll_i}>Info</option>
<option value="warning" {ll_w}>Warning</option>
<option value="error" {ll_e}>Error</option></select></div>
<div><label>Port dashboard</label><input type="number" name="port" value="{port}"></div>
<div><label>Auth active</label><select name="auth_enabled"><option value="true" {auth_t}>Oui</option><option value="false" {auth_f}>Non</option></select></div>
<div><label>Email login</label><input type="email" name="login_email" value="{login_email}"></div>
<div><label>Mot de passe login</label><input type="password" name="login_password" value="{login_password}"></div>
<div><label>Session secret</label><input type="text" name="session_secret" value="{session_secret}"></div>
<div><label>Duree session (heures)</label><input type="number" name="session_max_age_hours" value="{session_max_hours}"></div>
<div><label>Host</label><input type="text" name="host" value="{host}"></div>
</div>
<div style="margin-top:16px">
<button type="submit" class="btn btn-primary">Sauvegarder</button>
<button type="button" class="btn btn-warning" onclick="applyConfig()" style="margin-left:8px">Appliquer (recharger)</button>
</div>
</form>
<h2 style="margin-top:24px">Export / Import Configuration</h2>
<div style="margin-top:8px">
<a href="/api/config/export" class="btn btn-success" download="config.json">Exporter config.json</a>
<button type="button" class="btn btn-primary" onclick="document.getElementById('import-file').click()" style="margin-left:8px">Importer config.json</button>
<input type="file" id="import-file" accept=".json" style="display:none" onchange="importConfig(this)">
</div>
<div id="import-status" style="margin-top:8px"></div>
</div>""".format(
        ll_d=_s(log_level, "debug"), ll_i=_s(log_level, "info"),
        ll_w=_s(log_level, "warning"), ll_e=_s(log_level, "error"),
        port=_esc(dash.get("port", 3000)),
        auth_t=_s(dash.get("auth_enabled"), True), auth_f=_s(dash.get("auth_enabled"), False),
        login_email=_esc(dash.get("login_email", "")), login_password=_esc(dash.get("login_password", "")),
        session_secret=_esc(dash.get("session_secret", "")),
        session_max_hours=_esc(dash.get("session_max_age_hours", 24)),
        host=_esc(dash.get("host", "0.0.0.0")),
    )

    body += """
<script>
function testDb(){document.getElementById("db-test").textContent="Test en cours...";api("GET","/api/db/test").then(function(d){document.getElementById("db-test").innerHTML=d.ok?'<span class="badge badge-ok">Connecte: '+d.table_map.documents+'</span>':'<span class="badge badge-err">'+d.error+'</span>'})}
function testSfec(){document.getElementById("sfec-test").textContent="Test en cours...";api("GET","/api/sfec/test").then(function(d){document.getElementById("sfec-test").innerHTML=d.connected?'<span class="badge badge-ok">Connecte: '+d.url+'</span>':'<span class="badge badge-err">Deconnecte</span>'})}
function applyConfig(){showToast("Application de la configuration...","info");api("POST","/api/config/reload").then(function(d){showToast(d.message||"OK","ok");}).catch(function(e){showToast("Erreur: "+e,"err")})}
function importConfig(input){var file=input.files[0];if(!file)return;var reader=new FileReader();reader.onload=function(e){try{var cfg=JSON.parse(e.target.result);document.getElementById("import-status").textContent="Importation...";api("POST","/api/config/import",cfg).then(function(d){if(d.ok){showToast("Config importee! Rechargement...","ok");setTimeout(function(){location.reload()},1500)}else{showToast("Erreur: "+d.error,"err")}})}catch(err){showToast("JSON invalide: "+err,"err")}};reader.readAsText(file)}
</script>"""

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
            import article_sync
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

    stat_cards = """<div class="card"><h2>Facturation</h2><div class="stat-grid">
<div class="stat"><div class="value">{total}</div><div class="label">Total factures</div></div>
<div class="stat"><div class="value">{brouillons}</div><div class="label">Brouillons</div></div>
<div class="stat"><div class="value">{validees}</div><div class="label">Validees</div></div>
<div class="stat"><div class="value">{certifiees}</div><div class="label">Certifiees SFEC</div></div>
<div class="stat"><div class="value">{certif_en_cours}</div><div class="label">Certif en attente</div></div>
<div class="stat"><div class="value">{certif_erreur}</div><div class="label">Certif en erreur</div></div>
<div class="stat"><div class="value">{ca:,.0f}</div><div class="label">CA Total TTC</div></div>
<div class="stat"><div class="value">{nb_imp:,.0f}</div><div class="label">Impayes (FCFA)</div></div>
<div class="stat"><div class="value">{pending_sage}</div><div class="label">A pousser Sage</div></div>
<div class="stat"><div class="value">{pushed}</div><div class="label">Sync Sage OK</div></div>
</div>""".format(
        total=stats.get("total", 0), brouillons=stats.get("brouillons", 0),
        validees=stats.get("validees", 0), certifiees=stats.get("certifiees", 0),
        ca=stats.get("ca_total", 0), pushed=bi_stats.get("pushed", 0),
        certif_en_cours=stats.get("certif_en_cours", 0), certif_erreur=stats.get("certif_erreur", 0),
        nb_imp=stats.get("impayes_total", 0), pending_sage=stats.get("pending_sage", 0)
    )

    mois_cards = """<div class="card"><h2>Mois en cours</h2><div class="stat-grid">
<div class="stat"><div class="value">{mois_nb}</div><div class="label">Factures du mois</div></div>
<div class="stat"><div class="value">{mois_ht:,.0f}</div><div class="label">HT (FCFA)</div></div>
<div class="stat"><div class="value">{mois_tva:,.0f}</div><div class="label">TVA (FCFA)</div></div>
<div class="stat"><div class="value">{mois_ttc:,.0f}</div><div class="label">TTC (FCFA)</div></div>
</div></div>""".format(
        mois_nb=stats.get("mois_nb", 0), mois_ht=stats.get("mois_ht", 0),
        mois_tva=stats.get("mois_tva", 0), mois_ttc=stats.get("mois_ttc", 0)
    )

    filters_card = """<div class="card no-print"><h2>Filtres</h2>
<form id="filters-form" style="display:flex;gap:12px;flex-wrap:wrap;align-items:end">
<div><label>Date debut</label><input type="date" id="f_date_from"></div>
<div><label>Date fin</label><input type="date" id="f_date_to"></div>
<div><label>Statut</label><select id="f_statut">{statut_opts}</select></div>
<div><label>Source</label><select id="f_source">{source_opts}</select></div>
<div><label>N facture</label><input type="text" id="f_search" placeholder="FA000002..." style="min-width:150px"></div>
<div><button type="submit" class="btn btn-primary">Filtrer</button></div>
<div><button type="button" class="btn btn-sm" onclick="resetFilters()">Reinitialiser</button></div>
</form></div>""".format(statut_opts=statut_opts, source_opts=source_opts)

    actions_card = """<div class="card no-print"><h2>Actions</h2>
<a href="/billing/invoice/new" class="btn btn-primary">Nouvelle Facture</a>
<button class="btn btn-success" onclick="syncBi()" style="margin-left:8px">Sync Sage</button>
<span style="margin-left:8px;font-size:12px;color:#64748b">Derniere sync: {last_sync}</span>
</div>""".format(
        last_sync=bi_stats.get("last_sync", "jamais")[:19].replace("T", " ") if bi_stats.get("last_sync") else "jamais"
    )

    table_card = """<div class="card"><h2>Factures (<span id="inv-count">0</span>)</h2>
<table><thead><tr>
<th data-sort="numero" onclick="toggleSort('numero')">Numero<span id="sort_numero"></span></th>
<th data-sort="date_facture" onclick="toggleSort('date_facture')">Date<span id="sort_date_facture"></span></th>
<th data-sort="tiers_nom" onclick="toggleSort('tiers_nom')">Tiers<span id="sort_tiers_nom"></span></th>
<th style="text-align:right">HT</th><th style="text-align:right">TVA</th><th style="text-align:right">TTC</th>
<th style="text-align:right">Restant</th>
<th data-sort="statut" onclick="toggleSort('statut')">Statut<span id="sort_statut"></span></th>
<th data-sort="source" onclick="toggleSort('source')">Source<span id="sort_source"></span></th>
<th>SFEC</th><th>Sage</th><th>Actions</th>
</tr></thead>
<tbody id="inv-body"><tr><td colspan="12" style="text-align:center;color:#64748b">Chargement...</td></tr></tbody>
</table>
<div id="inv-pagination" style="margin-top:10px"></div></div>"""
    script_block = """<script>
window.NATIVE_ALERTS = true;
var bs={page:1,limit:25,sort_by:"date_facture",sort_dir:"DESC"};
function esc(x){var d=document.createElement("div");d.textContent=(x==null?"":String(x));return d.innerHTML;}
function fmt(x){return Number(x||0).toLocaleString("fr-FR",{maximumFractionDigits:0});}
function buildQ(){
var q=[],a=function(k,v){if(v!==null&&v!==""){q.push(k+"="+encodeURIComponent(v));}};
a("statut",document.getElementById("f_statut").value);
a("source",document.getElementById("f_source").value);
a("search",document.getElementById("f_search").value);
a("date_from",document.getElementById("f_date_from").value);
a("date_to",document.getElementById("f_date_to").value);
a("sort_by",bs.sort_by);a("sort_dir",bs.sort_dir);
a("page",bs.page);a("limit",bs.limit);
return q.join("&");}
function sfecCell(t){
var ss=t.sfec_statut||"",cb=' <button class="btn btn-sm" onclick="certifyBilling('+t.id+')">Certifier</button>';
if(ss==="CERTIFIE"||ss==="DEJA_CERTIFIE"){return '<span class="badge badge-ok" title="'+esc((t.sfec_num_certif||"").slice(0,25))+'">'+(ss==="CERTIFIE"?"Certifie":"Deja certifie")+"</span>";}
if(ss==="EN_COURS"){return '<span class="badge badge-warn">En attente</span>'+cb;}
if(ss==="ERREUR"){return '<span class="badge badge-err">Erreur</span>'+cb;}
return '<span class="badge badge-info">A certifier</span>'+cb;}
function sageCell(t){
if(t.synced_sage){return '<span class="badge badge-ok" title="'+esc(t.sage_piece||"")+'">Dans Sage</span>';}
if(t.source==="sage"){return '<span class="badge badge-info">Importe</span>';}
var pushable=(t.statut!=="brouillon")&&(t.source!=="pos"||(t.sfec_num_certif||""));
return '<span class="badge badge-warn">En attente</span>'+(pushable?' <button class="btn btn-sm" onclick="pushInvoice('+t.id+')">Pousser</button>':"");}
function invRow(t){
var s=t.statut||"";
var sb=(s==="valide"||s==="a_comptabilise")?"badge-ok":(s==="a_comptabiliser")?"badge-warn":(s==="brouillon")?"badge-info":"";
var rest=t.montant_restant||0,u="/billing/invoice/"+t.id;
return '<tr><td><a href="'+u+'" style="color:#38bdf8;text-decoration:none">'+esc(t.numero)+'</a></td>'
+'<td>'+esc(t.date_facture)+'</td><td>'+esc(t.tiers_nom)+'</td>'
+'<td style="text-align:right">'+fmt(t.montant_ht)+'</td><td style="text-align:right">'+fmt(t.montant_tva)+'</td><td style="text-align:right">'+fmt(t.montant_ttc)+'</td>'
+'<td style="text-align:right">'+fmt(rest)+'</td>'
+'<td><span class="badge '+sb+'">'+esc(s)+'</span></td>'
+'<td><span class="badge badge-info">'+esc(t.source)+'</span></td>'
+'<td>'+sfecCell(t)+'</td><td>'+sageCell(t)+'</td>'
+'<td><a class="btn btn-sm" href="'+u+'" target="_blank" style="text-decoration:none">Voir</a></td></tr>';}
function setArrows(){
var k=["numero","date_facture","tiers_nom","statut","source"],i,el;
for(i=0;i<k.length;i++){el=document.getElementById("sort_"+k[i]);if(el){el.textContent=(bs.sort_by===k[i])?(bs.sort_dir==="ASC"?String.fromCharCode(0x25B2):String.fromCharCode(0x25BC)):"";}}}
function paginate(d){
var p=document.getElementById("inv-pagination");
if(d.pages<=1){p.innerHTML="";return;}
var html='<span style="color:#64748b">Page '+d.page+' / '+d.pages+'</span> ';
if(d.page>1){html+='<button class="btn btn-sm" onclick="go('+(d.page-1)+')">Prec</button> ';}
var w=2,start=Math.max(1,d.page-w),end=Math.min(d.pages,d.page+w),i;
for(i=start;i<=end;i++){html+=(i===d.page)?'<span class="badge badge-ok">'+i+'</span> ':'<button class="btn btn-sm" onclick="go('+i+')">'+i+'</button> ';}
if(d.page<d.pages){html+='<button class="btn btn-sm" onclick="go('+(d.page+1)+')">Suiv</button>';}
html+=' <select onchange="setLimit(this.value)">';
var Ls=[25,50,100];
for(i=0;i<Ls.length;i++){html+='<option value="'+Ls[i]+'"'+(Ls[i]===d.limit?' selected':'')+'>'+Ls[i]+' / page</option>';}
html+='</select>';
p.innerHTML=html;}
function render(){
setArrows();
api("GET","/api/invoices/list?"+buildQ()).then(function(d){
var tb=document.getElementById("inv-body");tb.innerHTML="";
if(!d.invoices.length){tb.innerHTML='<tr><td colspan="12" style="text-align:center;color:#64748b">Aucune facture</td></tr>';}
else{for(var i=0;i<d.invoices.length;i++){tb.innerHTML+=invRow(d.invoices[i]);}}
document.getElementById("inv-count").textContent=d.total;paginate(d);
}).catch(function(e){showToast("Erreur: "+e,"err");});}
function go(n){bs.page=n;render();}
function setLimit(n){bs.limit=Number(n);bs.page=1;render();}
function toggleSort(k){if(bs.sort_by===k){bs.sort_dir=bs.sort_dir==="DESC"?"ASC":"DESC";}else{bs.sort_by=k;bs.sort_dir="DESC";}bs.page=1;render();}
function resetFilters(){["f_statut","f_source","f_search","f_date_from","f_date_to"].forEach(function(id){document.getElementById(id).value="";});bs.page=1;render();}
function syncBi(){api("POST","/api/sync/bi",{}).then(function(d){showToast("Sync: "+d.pushed+" poussees, "+d.pull_new+" tirees","ok");render()}).catch(function(e){showToast("Erreur: "+e,"err")});}
function certifyBilling(id){api("POST","/api/sfec/certify",{invoice_id:"INV-"+id}).then(function(r){if(r.success&&r.certification_number){showToast("Certifiee SFEC: "+r.certification_number,"ok");render();}else if(r.success){showToast("Certification envoyee, en attente...","warn");render();}else{showToast("Certification: "+(r.error||"echec"),"err");}})}
function pushInvoice(id){api("POST","/api/invoices/"+id+"/push-sage").then(function(r){if(r.success){showToast("Facture poussee vers Sage","ok");render();}else{showToast("Push Sage: "+(r.error||"echec"),"err");}})}
document.getElementById("f_search").addEventListener("input",(function(){var t;return function(){clearTimeout(t);t=setTimeout(function(){bs.page=1;render();},250);};})());
document.getElementById("filters-form").addEventListener("submit",function(e){e.preventDefault();bs.page=1;render();});
["f_statut","f_source","f_date_from","f_date_to"].forEach(function(id){document.getElementById(id).addEventListener("change",function(){bs.page=1;render();});});
render();
</script>"""
    body = stat_cards + mois_cards + filters_card + actions_card + table_card + script_block
    return _page(body)


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
    body = """
<div class="card"><h2>Facture {}</h2>
<div style="margin-bottom:12px">
<a href="/billing" class="btn btn-sm">Retour</a>
<a href="/billing/invoice/{}/edit" class="btn btn-sm btn-primary" style="margin-left:8px">Modifier</a>
<button class="btn btn-sm btn-success" onclick="certifyInv({})" style="margin-left:8px">Certifier SFEC</button>
<a href="/api/invoices/{}/pdf" class="btn btn-sm btn-primary" style="margin-left:8px;text-decoration:none" target="_blank">PDF</a>
<button class="btn btn-sm btn-danger" onclick="if(confirm('Supprimer ?')){{api('DELETE','/api/invoices/{}').then(function(){{location.href='/billing'}})}}" style="margin-left:8px">Supprimer</button>
</div>
<table>
<tr><td>Numero</td><td><strong>{}</strong></td></tr>
<tr><td>Date</td><td>{}</td></tr>
<tr><td>Reference</td><td>{}</td></tr>
<tr><td>Tiers</td><td>{} ({})</td></tr>
<tr><td>Statut</td><td><span class="badge badge-ok">{}</span></td></tr>
<tr><td>Source</td><td>{}</td></tr>
{}
<tr><td>Notes</td><td>{}</td></tr>
</table>
<h3 style="margin-top:16px;color:#38bdf8">Lignes</h3>
<table><thead><tr><th>Designation</th><th style='text-align:right'>Qte</th><th style='text-align:right'>Prix unit.</th><th style='text-align:right'>TVA</th><th style='text-align:right'>HT</th><th style='text-align:right'>TVA</th><th style='text-align:right'>TTC</th></tr></thead>
<tbody>{}</tbody></table>
<div style="text-align:right;margin-top:12px;font-size:18px">
<strong>Total HT: {:,.0f} FCFA</strong><br>
<strong>TVA: {:,.0f} FCFA</strong><br>
<strong style="color:#38bdf8">TOTAL TTC: {:,.0f} FCFA</strong>
</div></div>
<script>
window.NATIVE_ALERTS = true;
function certifyInv(id){{api("POST","/api/sfec/certify",{{invoice_id:"INV-"+id}}).then(function(d){{if(d.success){{showToast("Certifie: "+d.certification_number,"ok");setTimeout(function(){{location.reload()}},1500)}}else{{showToast("Erreur: "+d.error,"err")}}}})}}
</script>""".format(
        _esc(inv.get("numero", "")),
        inv["id"], inv["id"], inv["id"], inv["id"],
        _esc(inv.get("numero", "")), _esc(inv.get("date_facture", "")),
        _esc(inv.get("reference", "") or "-"), _esc(inv.get("tiers_nom", "") or "N/A"),
        _esc(inv.get("tiers_code", "") or ""), _esc(inv.get("statut", "")),
        _esc(inv.get("source", "web")),
        sfec_html, _esc(inv.get("notes", "") or "-"),
        lignes_rows, inv.get("montant_ht", 0), inv.get("montant_tva", 0), inv.get("montant_ttc", 0)
    )
    return _page(body)


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

    body = """
<div class="page-header">
  <div>
    <div class="breadcrumb"><a href="/billing">Facturation</a> / {title}</div>
    <h1>{title}</h1>
  </div>
  <div>
    <a href="/billing" class="btn btn-sm btn-primary">Retour</a>
  </div>
</div>

<form id="form-invoice">
  <div class="invoice-layout">
    <div class="invoice-main">
      <!-- Invoice Header -->
      <div class="invoice-header-card">
        <div class="section-title">Informations facture</div>
        <div class="invoice-meta">
          <div><label>Numero</label><input type="text" name="numero" value="{numero}" {readonly}></div>
          <div><label>Date facture</label><input type="date" name="date_facture" value="{date_facture}"></div>
          <div><label>Echeance</label><input type="date" name="date_echeance" value="{date_echeance}"></div>
          <div><label>Reference</label><input type="text" name="reference" value="{reference}"></div>
          <div><label>Statut</label><select name="statut">
            <option value="brouillon" {s_brouillon}>Brouillon</option>
            <option value="valide" {s_valide}>Valide</option>
            <option value="a_comptabiliser" {s_acompt}>A comptabiliser</option>
          </select></div>
          <div><label>Type tiers</label><select name="tiers_type" onchange="onTiersTypeChange()">
            <option value="business" {t_bus}>Entreprise</option>
            <option value="individual" {t_ind}>Particulier</option>
            <option value="government" {t_gov}>Gouvernement</option>
            <option value="foreign" {t_for}>Etranger</option>
          </select>
          <div class="tiers-type-info" id="tiers-type-info"></div></div>
        </div>
      </div>

      <!-- Client Section -->
      <div class="client-card">
        <div class="section-title">Client / Tiers</div>
        <div class="grid-2">
          <div class="contact-select-wrapper">
            <label>Selectionner un contact</label>
            <select id="contact-select" onchange="fillContact()">
              <option value="">-- Choisir --</option>
            </select>
          </div>
          <div><label>Code tiers</label><input type="text" id="tiers_code" name="tiers_code" value="{tiers_code}"></div>
          <div><label>Nom</label><input type="text" id="tiers_nom" name="tiers_nom" value="{tiers_nom}"></div>
          <div id="niu-field"><label id="niu-label">NIU</label><input type="text" id="tiers_niu" name="tiers_niu" value="{tiers_niu}"><div class="niu-required-hint" id="niu-hint" style="display:none"></div></div>
          <div><label>Email</label><input type="email" id="tiers_email" name="tiers_email" value="{tiers_email}"></div>
          <div><label>Telephone</label><input type="text" id="tiers_telephone" name="tiers_telephone" value="{tiers_telephone}"></div>
          <div class="full-width"><label>Adresse</label><input type="text" id="tiers_adresse" name="tiers_adresse" value="{tiers_adresse}"></div>
        </div>
      </div>

      <!-- Line Items -->
      <div style="margin-top:20px">
        <div class="section-title">Lignes de facture</div>
        <div id="lines-container"></div>
        <button type="button" class="btn btn-sm" onclick="addLine()" style="margin-top:10px">+ Ajouter une ligne</button>
      </div>

      <!-- Notes -->
      <div style="margin-top:20px">
        <div class="section-title">Notes</div>
        <textarea name="notes" rows="3" style="width:100%;background:#fff;border:1.5px solid #e2e8f0;color:#1e293b;padding:10px 14px;border-radius:8px;font-size:13px;font-family:inherit;resize:vertical">{notes}</textarea>
      </div>

      <!-- Actions -->
      <div class="form-actions">
        <button type="submit" class="btn btn-primary">{btn_text}</button>
        <button type="button" class="btn btn-success" onclick="saveAndCertify()">Enregistrer & Certifier SFEC</button>
      </div>
    </div>

    <!-- Sidebar Totals -->
    <div class="invoice-sidebar">
      <div class="totals-panel">
        <h3>Recapitulatif</h3>
        <div class="total-row">
          <span class="total-label">Total HT</span>
          <span class="total-value"><span id="total-ht">0</span> FCFA</span>
        </div>
        <div class="total-row">
          <span class="total-label">TVA</span>
          <span class="total-value"><span id="total-tva">0</span> FCFA</span>
        </div>
        <div class="total-row grand-total">
          <span class="total-label">TOTAL TTC</span>
          <span class="total-value"><span id="total-ttc">0</span> FCFA</span>
        </div>
      </div>
    </div>
  </div>
</form>
<script>
window.NATIVE_ALERTS = true;
var contacts={contacts_json};
var products={products_json};
var taxRates={tax_rates_json};
var existingLines={lignes_json};
var isEdit={is_edit};
var invoiceId={inv_id};
var lineCounter=0;

function onTiersTypeChange(){{var t=document.querySelector("select[name='tiers_type']").value;
var info=document.getElementById("tiers-type-info");
var labels={{business:"Entreprise",individual:"Particulier",government:"Gouvernement",foreign:"Etranger"}};
info.textContent="Type SFEC: "+((labels[t]||t));
if(t==="business"||t==="government"){{document.getElementById("niu-field").classList.add("niu-required");document.getElementById("niu-label").innerHTML='NIU <span class="niu-star">*</span>';var h=document.getElementById("niu-hint");h.textContent="NIU obligatoire pour la certification SFEC (16-17 caracteres)";h.style.display="block"}}
else{{document.getElementById("niu-field").classList.remove("niu-required");document.getElementById("niu-label").textContent="NIU";document.getElementById("niu-hint").style.display="none"}}}}

function fillContact(){{var sel=document.getElementById("contact-select");var c=contacts.find(function(x){{return x.code===sel.value}});if(c){{document.getElementById("tiers_code").value=c.code;document.getElementById("tiers_nom").value=c.nom;document.getElementById("tiers_niu").value=c.niu||"";document.getElementById("tiers_email").value=c.email||"";document.getElementById("tiers_telephone").value=c.telephone||"";document.getElementById("tiers_adresse").value=c.adresse||"";var rt=c.type_raw?String(c.type_raw).toLowerCase():"";if(!rt&&c.type){{rt=String(c.type).toLowerCase();if(rt==="client")rt="business";if(rt==="fournisseur")rt="business"}}if(rt==="government"||rt==="gouvernement")rt="government";if(rt==="particulier"||rt==="individual"||rt==="individu")rt="individual";if(rt==="etranger"||rt==="foreign")rt="foreign";if(rt)document.querySelector("select[name='tiers_type']").value=rt;onTiersTypeChange()}}}}

function initContactSelect(){{var cs=document.getElementById("contact-select");cs.innerHTML='<option value="">-- Choisir --</option>';contacts.forEach(function(c){{var o=document.createElement("option");o.value=c.code;o.textContent=c.nom+" ("+c.code+")";cs.appendChild(o)}})}}

function validateTiersNiu(){{var t=document.querySelector("select[name='tiers_type']").value;var niu=document.getElementById("tiers_niu").value.trim();if(t==="business"||t==="government"){{if(!niu){{showToast("NIU obligatoire pour la certification SFEC (type: "+t+")","err");document.getElementById("tiers_niu").focus();return false}}}}return true}}

function addLine(data){{lineCounter++;var idx=lineCounter;var html='<div class="line-item" id="line-'+idx+'"><div class="line-item-header"><span class="line-item-title">Ligne #'+idx+'</span><div class="line-item-actions"><button type="button" class="btn btn-sm btn-danger" onclick="removeLine('+idx+')">Supprimer</button></div></div><div class="line-item-grid"><div><label>Designation</label><input type="text" name="l_design_'+idx+'" value="'+(data?data.designation:"")+'" list="products-list"></div><div><label>Qte</label><input type="number" name="l_qte_'+idx+'" value="'+(data?data.quantite:1)+'" step="0.01" min="0" onchange="calcLine('+idx+')"></div><div><label>Prix unitaire</label><input type="number" name="l_prix_'+idx+'" value="'+(data?data.prix_unitaire:0)+'" step="0.01" min="0" onchange="calcLine('+idx+')"></div><div><label>TVA %</label><select name="l_tva_'+idx+'" onchange="calcLine('+idx+')"></select></div><div><label>HT</label><input type="text" id="l_ht_'+idx+'" readonly value="'+(data?data.montant_ht:0)+'"></div><div><label>TTC</label><input type="text" id="l_ttc_'+idx+'" readonly value="'+(data?data.montant_ttc:0)+'"></div></div><input type="hidden" name="l_code_article_'+idx+'" value="'+(data?data.code_article:"")+'"><input type="hidden" name="l_famille_'+idx+'" value="'+(data?data.famille:"")+'"></div>';
document.getElementById("lines-container").insertAdjacentHTML("beforeend",html);
var sel=document.querySelector("select[name='l_tva_"+idx+"']");taxRates.forEach(function(t){{sel.innerHTML+='<option value="'+t.taux+'"'+(data&&data.taux_tva==t.taux?" selected":"")+'>'+t.code+' ('+t.taux+'%)</option>'}});
if(data)calcLine(idx)}}

function removeLine(idx){{var el=document.getElementById("line-"+idx);if(el)el.remove();calcTotals()}}

function calcLine(idx){{var q=parseFloat(document.querySelector("input[name='l_qte_"+idx+"']").value)||0;var p=parseFloat(document.querySelector("input[name='l_prix_"+idx+"']").value)||0;var t=parseFloat(document.querySelector("select[name='l_tva_"+idx+"']").value)||0;var ht=round2(q*p);var tva=round2(ht*t/100);var ttc=round2(ht+tva);document.getElementById("l_ht_"+idx).value=ht;document.getElementById("l_ttc_"+idx).value=ttc;calcTotals()}}

function calcTotals(){{var th=0,tt=0;document.querySelectorAll("[id^='l_ht_']").forEach(function(el){{th+=parseFloat(el.value)||0}});document.querySelectorAll("[id^='l_ttc_']").forEach(function(el){{tt+=parseFloat(el.value)||0}});document.getElementById("total-ht").textContent=round2(th).toLocaleString();document.getElementById("total-tva").textContent=round2(tt-th).toLocaleString();document.getElementById("total-ttc").textContent=round2(tt).toLocaleString()}}
function round2(n){{return Math.round(n*100)/100}}

function gatherData(){{var d={{}};d.date_facture=document.querySelector("input[name='date_facture']").value;d.date_echeance=document.querySelector("input[name='date_echeance']").value;d.reference=document.querySelector("input[name='reference']").value;d.tiers_code=document.getElementById("tiers_code").value;d.tiers_nom=document.getElementById("tiers_nom").value;d.tiers_niu=document.getElementById("tiers_niu").value;d.tiers_email=document.getElementById("tiers_email").value;d.tiers_telephone=document.getElementById("tiers_telephone").value;d.tiers_adresse=document.getElementById("tiers_adresse").value;d.tiers_type=document.querySelector("select[name='tiers_type']").value;d.statut=document.querySelector("select[name='statut']").value;d.notes=document.querySelector("textarea[name='notes']").value;d.lignes=[];var lines=document.querySelectorAll("[id^='line-']");lines.forEach(function(el){{var idx=el.id.replace("line-","");var ligne={{}};ligne.designation=document.querySelector("input[name='l_design_"+idx+"']").value;ligne.quantite=parseFloat(document.querySelector("input[name='l_qte_"+idx+"']").value)||1;ligne.prix_unitaire=parseFloat(document.querySelector("input[name='l_prix_"+idx+"']").value)||0;ligne.taux_tva=parseFloat(document.querySelector("select[name='l_tva_"+idx+"']").value)||18;ligne.code_article=document.querySelector("input[name='l_code_article_"+idx+"']").value;ligne.famille=document.querySelector("input[name='l_famille_"+idx+"']").value;if(ligne.designation)d.lignes.push(ligne)}});return d}}

document.getElementById("form-invoice").onsubmit=function(e){{e.preventDefault();if(!validateTiersNiu())return;var d=gatherData();var url=isEdit?"/api/invoices/"+invoiceId:"/api/invoices";var method=isEdit?"PUT":"POST";api(method,url,d).then(function(r){{if(r.success||r.id){{showToast("Facture sauvegardee","ok");setTimeout(function(){{location.href="/billing/invoice/"+(r.id||invoiceId)}},1000)}}else{{showToast("Erreur: "+(r.error||"inconnue"),"err")}}}}).catch(function(e){{showToast("Erreur reseau: "+e,"err")}})}}

function saveAndCertify(){{if(!validateTiersNiu())return;var d=gatherData();d._certify=true;var url=isEdit?"/api/invoices/"+invoiceId:"/api/invoices";var method=isEdit?"PUT":"POST";api(method,url,d).then(function(r){{if(r.id){{api("POST","/api/sfec/certify",{{invoice_id:"INV-"+r.id}}).then(function(c){{if(c.success){{showToast("Certifie: "+c.certification_number,"ok")}}else{{showToast("Certification: "+c.error,"warn")}}setTimeout(function(){{location.href="/billing/invoice/"+r.id}},1500)}})}}}})}}

if(existingLines.length>0){{existingLines.forEach(function(l){{addLine(l)}})}}
else{{addLine()}}
initContactSelect();
onTiersTypeChange();
</script>
<datalist id="products-list"></datalist>
""".format(
        title=title, numero=_esc(numero), date_facture=date_facture, date_echeance=date_echeance,
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
        lignes_json=lignes_json, is_edit="true" if is_edit else "false",
        inv_id=inv["id"] if is_edit else "null",
        btn_text="Modifier" if is_edit else "Enregistrer"
    )
    return _page(body)


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

    pos_css = """<style>
.pos-layout{--posc-1:#4f46e5;--posc-2:#7c3aed;--posc-ink:#0f172a;--posc-sub:#64748b;--posc-line:#e6e9f2;
  display:grid;grid-template-columns:1fr 420px;gap:20px;align-items:start}
.pos-main{min-width:0}
.pos-side{position:sticky;top:24px;display:flex;flex-direction:column;gap:18px}

.pos-card{background:#fff;border:1px solid var(--posc-line);border-radius:18px;padding:22px;box-shadow:0 1px 2px rgba(15,23,42,.04),0 20px 40px -28px rgba(15,23,42,.28)}

.pos-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:12px;flex-wrap:wrap}
.pos-head h2{display:flex;align-items:center;gap:10px;font-size:17px;font-weight:800;color:var(--posc-ink);letter-spacing:-.2px;margin:0}
.pos-head h2 .ic{width:34px;height:34px;flex-shrink:0;border-radius:10px;background:linear-gradient(135deg,var(--posc-1),var(--posc-2));display:flex;align-items:center;justify-content:center;color:#fff;box-shadow:0 6px 16px rgba(79,70,229,.32)}
.pos-head h2 .ic svg{width:17px;height:17px}
.pos-head-actions{display:flex;gap:8px}

.pos-stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
.pos-stat-pill{flex:1;min-width:130px;background:#f8fafc;border:1px solid var(--posc-line);border-radius:12px;padding:10px 14px}
.pos-stat-pill b{display:block;font-size:16px;font-weight:800;color:var(--posc-ink);letter-spacing:-.3px}
.pos-stat-pill span{font-size:10px;color:var(--posc-sub);font-weight:700;text-transform:uppercase;letter-spacing:.6px}

.pos-scanner{display:flex;gap:14px;align-items:center;background:linear-gradient(135deg,#111827,#1e293b);border:1px solid #263449;border-radius:14px;padding:12px 16px;margin-bottom:16px;position:relative;overflow:hidden}
.pos-scanner::before{content:'';position:absolute;inset:0;background:radial-gradient(560px 120px at 8% -40%,rgba(124,58,237,.28),transparent 60%);pointer-events:none}
.pos-scanner .sc-icon{width:40px;height:40px;flex-shrink:0;border-radius:11px;background:rgba(129,140,248,.14);border:1px solid rgba(129,140,248,.25);color:#a5b4fc;display:flex;align-items:center;justify-content:center;position:relative}
.pos-scanner .sc-icon svg{width:20px;height:20px}
.pos-scanner .sc-body{flex:1;min-width:0;position:relative}
.pos-scanner .sc-input-wrap{position:relative;display:flex;align-items:center}
.pos-scanner input{width:100%;background:rgba(15,23,42,.6);border:1.5px solid #334155;color:#f1f5f9;font-size:14.5px;font-weight:600;letter-spacing:.3px;text-align:left;padding:11px 74px 11px 14px;border-radius:10px;font-family:inherit;transition:border-color .15s,background .15s}
.pos-scanner input:focus{outline:none;border-color:#818cf8;background:rgba(15,23,42,.85);box-shadow:0 0 0 4px rgba(129,140,248,.18)}
.pos-scanner input::placeholder{color:#67748f;font-weight:500;letter-spacing:.2px}
.pos-scanner .sc-kbd{position:absolute;right:8px;top:50%;transform:translateY(-50%);display:flex;align-items:center;gap:3px;background:rgba(129,140,248,.14);border:1px solid rgba(129,140,248,.25);color:#c7d2fe;font-size:10px;font-weight:700;padding:4px 8px;border-radius:6px;letter-spacing:.3px;pointer-events:none}
.pos-scanner .sc-kbd svg{width:11px;height:11px}
.pos-scanner .hint{color:#8593ac;font-size:10.5px;margin-top:7px;letter-spacing:.2px;display:flex;align-items:center;gap:6px}
.pos-scanner .hint .dot{width:6px;height:6px;border-radius:50%;background:#34d399;flex-shrink:0;box-shadow:0 0 0 3px rgba(52,211,153,.18);animation:scPulse 1.8s ease-in-out infinite}
@keyframes scPulse{0%,100%{opacity:1}50%{opacity:.4}}

.pos-toolbar{display:flex;gap:12px;align-items:center;margin-bottom:6px;flex-wrap:wrap}
.pos-qte-mini{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--posc-sub);font-weight:700;white-space:nowrap}
.pos-qte-mini input{width:64px;padding:7px 8px;text-align:center;font-weight:700}
.pos-search{position:relative;flex:1;min-width:220px}
.pos-search svg{position:absolute;left:14px;top:50%;transform:translateY(-50%);width:16px;height:16px;color:#94a3b8;pointer-events:none;flex-shrink:0}
.pos-search input{padding-left:40px;padding-right:14px;font-size:14px;border-radius:10px}

.pos-section-title{font-size:11.5px;font-weight:800;color:var(--posc-1);text-transform:uppercase;letter-spacing:.9px;margin:16px 0 10px;display:flex;align-items:center;gap:8px}
.pos-section-title::before{content:'';display:inline-block;width:4px;height:14px;background:linear-gradient(180deg,var(--posc-1),var(--posc-2));border-radius:2px}

.pos-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:12px}
.pos-tile{background:#fff;border:1px solid var(--posc-line);border-radius:14px;padding:14px;cursor:pointer;transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease;box-shadow:0 1px 2px rgba(15,23,42,.03);user-select:none;position:relative;overflow:hidden}
.pos-tile::after{content:'';position:absolute;left:0;right:0;bottom:0;height:3px;background:linear-gradient(90deg,var(--posc-1),var(--posc-2));transform:scaleX(0);transform-origin:left;transition:transform .18s ease}
.pos-tile:hover{border-color:#c7d2fe;box-shadow:0 14px 28px -14px rgba(79,70,229,.35);transform:translateY(-3px)}
.pos-tile:hover::after{transform:scaleX(1)}
.pos-tile:active{transform:translateY(-1px) scale(.98)}
.pos-tile-nom{font-size:13px;font-weight:700;color:var(--posc-ink);line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:6px;min-height:34px}
.pos-tile-ref{font-size:10px;color:#94a3b8;font-weight:700;margin-bottom:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-transform:uppercase;letter-spacing:.4px}
.pos-tile-foot{display:flex;justify-content:space-between;align-items:center;gap:6px}
.pos-tile-prix{font-size:15px;font-weight:800;color:var(--posc-1);letter-spacing:-.2px}
.pos-tile-stock{font-size:9.5px;font-weight:700;padding:2px 7px;border-radius:20px;background:#f0fdf4;color:#15803d;white-space:nowrap;flex-shrink:0}
.pos-tile-stock.low{background:#fffbeb;color:#b45309}
.pos-tile-stock.out{background:#fef2f2;color:#b91c1c}
.pos-tile .badge-sold{position:absolute;top:10px;right:10px;background:linear-gradient(135deg,#ede9fe,#ddd6fe);color:#6d28d9;font-size:9px;font-weight:800;padding:3px 8px;border-radius:8px;letter-spacing:.3px}

.pos-results{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:12px;margin-top:14px}
.pos-results-empty{padding:28px 16px;text-align:center;color:#94a3b8;font-size:13px;border:1.5px dashed var(--posc-line);border-radius:14px;background:#fafbfe}

.pos-client-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}

.pos-cart-table{width:100%;border-collapse:separate;border-spacing:0 8px;margin-top:-4px}
.pos-cart-table thead th{background:transparent;border:none;padding:0 10px 2px;font-size:9.5px}
.pos-cart-table tbody td{background:#f8fafc;border:none;padding:10px}
.pos-cart-table tbody tr td:first-child{border-radius:10px 0 0 10px}
.pos-cart-table tbody tr td:last-child{border-radius:0 10px 10px 0}
.pos-qty-stepper{display:inline-flex;align-items:center;gap:2px;background:#fff;border:1px solid var(--posc-line);border-radius:8px;padding:2px}
.pos-qty-stepper button{width:22px;height:22px;border:none;background:transparent;color:var(--posc-1);font-weight:800;font-size:14px;cursor:pointer;border-radius:6px;line-height:1;font-family:inherit}
.pos-qty-stepper button:hover{background:#eef2ff}
.pos-qty-stepper input{border:none!important;width:34px!important;padding:0!important;background:transparent!important;box-shadow:none!important;font-weight:700!important}

.pos-cart-empty{text-align:center;padding:36px 16px;color:#94a3b8;font-size:13px}
.pos-cart-empty svg{width:38px;height:38px;color:#cbd5e1;margin-bottom:10px}

.pos-total-box{display:flex;justify-content:space-between;align-items:center;background:linear-gradient(135deg,#0f172a,#1e293b);border-radius:14px;padding:16px 18px;margin-top:14px;box-shadow:0 12px 24px -14px rgba(15,23,42,.45)}
.pos-total-box .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:#94a3b8;font-weight:700}
.pos-total-box .val{font-size:24px;font-weight:800;color:#fff;letter-spacing:-.4px}
.pos-cart-foot{display:flex;justify-content:space-between;font-size:11.5px;color:var(--posc-sub);margin-top:8px;padding:0 2px}

.pos-pay-methods{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px}
.pos-pay-btn{display:flex;flex-direction:column;align-items:center;gap:5px;padding:10px 4px;border:1.5px solid var(--posc-line);border-radius:10px;background:#fff;cursor:pointer;font-size:10px;font-weight:700;color:var(--posc-sub);transition:all .15s;font-family:inherit}
.pos-pay-btn svg{width:16px;height:16px}
.pos-pay-btn:hover{border-color:#c7d2fe;background:#f5f6ff;color:var(--posc-1)}
.pos-pay-btn.active{border-color:var(--posc-1);background:linear-gradient(135deg,rgba(79,70,229,.08),rgba(124,58,237,.06));color:var(--posc-1);box-shadow:0 0 0 3px rgba(79,70,229,.1)}

.pos-pay-row{display:flex;gap:10px;align-items:flex-end;margin-top:12px}
.pos-pay-row>div{flex:1}
.pos-monnaie{font-size:14px;font-weight:800;margin-top:10px;text-align:right;padding:9px 12px;border-radius:9px}
.pos-monnaie:empty{padding:0;margin-top:0}
.pos-monnaie.ok{color:#059669;background:#f0fdf4}
.pos-monnaie.ko{color:#dc2626;background:#fef2f2}
.pos-validate{width:100%;padding:15px;font-size:15px;margin-top:14px;border-radius:12px;font-weight:800;letter-spacing:.2px}
.pos-last-row{display:grid;grid-template-columns:1fr auto auto;grid-template-rows:auto auto;column-gap:12px;align-items:center;padding:10px 6px;font-size:12.5px;border-bottom:1px solid #f1f5f9}
.pos-last-row:last-child{border-bottom:none}
.pos-last-row .num{font-weight:700;color:var(--posc-ink);grid-column:1;grid-row:1}
.pos-last-row .time{color:#94a3b8;font-size:11px;grid-column:1;grid-row:2}
.pos-last-row .amt{font-weight:800;color:var(--posc-1);grid-column:2;grid-row:1/3;justify-self:end}
.pos-last-row .pay{font-size:10px;color:var(--posc-sub);text-transform:capitalize;grid-column:3;grid-row:1/3;justify-self:end;background:#f1f5f9;padding:3px 8px;border-radius:20px;font-weight:700}

@media(max-width:1180px){.pos-layout{grid-template-columns:1fr}.pos-side{position:static}}
@media(max-width:600px){.pos-client-row{grid-template-columns:1fr}}
</style>"""

    body = pos_css + """
<div class="pos-layout">
<div class="pos-main">
<div class="pos-card">
<div class="pos-head">
<h2><span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg></span>Caisse</h2>
<div class="pos-head-actions">
<button class="btn btn-sm btn-ghost no-print" onclick="syncPosArticles()">Sync articles</button>
<button class="btn btn-sm btn-ghost no-print" id="btn-stock" onclick="toggleStock()">Stock: --</button>
</div>
</div>
<div class="pos-stats">@@STATS@@</div>
<div class="pos-scanner">
<div class="sc-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5V3h2v2H3zM7 5V3h4v2H7zM13 5V3h4v2h-4zM17 5V3h2v2h-2zM21 5h-2v2h2v14H3V5h2v14h14V7h2V5z"/><rect x="5" y="9" width="3" height="5"/><rect x="16" y="9" width="3" height="5"/><path d="M5 19v-2h3v2H5zM16 19v-2h3v2h-3z"/></svg></div>
<div class="sc-body">
<div class="sc-input-wrap">
<input id="pos-barcode" placeholder="Scanner ou saisir un code-barres..." autocomplete="off" autofocus
  onkeydown="if(event.key==='Enter'){event.preventDefault();scanBarcode(this.value)}">
<span class="sc-kbd"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg>Entree</span>
</div>
<div class="hint"><span class="dot"></span>Pret a scanner &mdash; l&#39;article est ajoute instantanement au ticket</div>
</div>
</div>
<div class="pos-toolbar">
<div class="pos-qte-mini">Qte par defaut <input type="number" id="pos-qte" value="1" min="1"></div>
<div class="pos-search">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
<input id="pos-search" placeholder="Rechercher un article (automatique des 2 caracteres)..." autocomplete="off" oninput="posInstantSearch(this.value)">
</div>
</div>
<div id="pos-products-head" class="pos-section-title">Meilleures ventes</div>
<div id="pos-products-grid" class="pos-grid">@@TILES@@</div>
<div id="pos-results"></div>
</div>
</div>
<div class="pos-side">
<div class="pos-card">
<div class="pos-head"><h2><span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4Z"/><path d="M3 6h18"/><path d="M16 10a4 4 0 0 1-8 0"/></svg></span>Ticket en cours</h2></div>
<div class="pos-client-row">
<div><label>Client</label><select id="pos-client">@@POS_CLIENTS@@</select></div>
<div><label>Vendeur</label><select id="pos-vendeur">@@VENDEURS@@</select></div>
</div>
<div id="pos-cart" style="margin-top:6px">
<table class="pos-cart-table"><thead><tr><th>Article</th><th style="text-align:center">Qte</th><th style="text-align:right">P. un.</th><th style="text-align:right">Total</th><th></th></tr></thead>
<tbody id="pos-cart-body"></tbody></table>
<div id="pos-cart-empty" class="pos-cart-empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg><br>Ticket vide<br>Ajoutez un article ci-contre ou scannez un code-barres</div>
</div>
<div class="pos-total-box"><span class="lbl">Total TTC</span><span class="val" id="pos-total">0 FCFA</span></div>
<div class="pos-cart-foot"><span>Articles : <b id="pos-articles-count">0</b></span><span>HT + TVA</span></div>
<div style="margin-top:14px">
<label>Mode de paiement</label>
<select id="pos-paiement" onchange="syncPayUI();calcMonnaie()" style="display:none">
<option value="especes" selected>Especes</option>
<option value="mobile_money">Mobile Money</option>
<option value="virement">Virement</option>
<option value="carte">Carte</option>
<option value="cheque">Cheque</option>
<option value="mixte">Mixte</option>
</select>
<div class="pos-pay-methods" id="pos-pay-methods"></div>
</div>
<div class="pos-pay-row">
<div><label>Recu (FCFA)</label><input type="number" id="pos-recu" value="0" min="0" oninput="calcMonnaie()"></div>
</div>
<div id="pos-monnaie" class="pos-monnaie"></div>
<button id="pos-validate" class="btn btn-success pos-validate" onclick="validerTicket()" disabled>Ticket vide</button>
<div style="margin-top:8px;text-align:center">
<button class="btn btn-sm btn-ghost" onclick="addPosLineManual()">+ Ligne manuelle</button>
<button class="btn btn-sm btn-ghost" onclick="clearCart()">Vider</button>
</div>
</div>
<div class="pos-card"><div class="pos-head"><h2 style="font-size:14px"><span class="ic" style="width:28px;height:28px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></span>Derniers tickets</h2></div>
<div>@@TICKETS@@</div></div>
</div>
</div>
<script>
var posCart=[];
var posLineIdx=0;
var posSearchTimer=null;
var posClients=@@POS_CLIENTS_JS@@;
window.NATIVE_ALERTS = true;

function _h(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}

function getPosClientData(v){
  v=String(v||"");
  if(v==="|CLI-CPT|Client comptoir"){var c=posClients.filter(function(x){return x.code==="CLI-CPT"})[0];return {code:"CLI-CPT",nom:"Client comptoir",id:c?c.id:null};}
  var parts=v.split("|");
  if(parts.length>=3){return {code:parts[1],nom:parts.slice(2).join("|"),id:null};}
  return {code:"",nom:"Client comptoir",id:null};
}

function addToCartFromTile(elm){
  addToCart({id:elm.getAttribute("data-id"),ref:elm.getAttribute("data-ref"),barcode:elm.getAttribute("data-barcode")||"",designation:elm.getAttribute("data-des"),prix_vente:parseFloat(elm.getAttribute("data-prix"))||0,tva_code:elm.getAttribute("data-tva")||"18"});
}

function addToCart(p){
  if(!p||!p.designation)return;
  var q=parseInt(document.getElementById("pos-qte").value)||1;if(q<1)q=1;
  var exist=null;
  if(p.ref){exist=posCart.filter(function(l){return l.ref&&l.ref===p.ref})[0]}
  if(exist){exist.quantite=round2(exist.quantite+q)}
  else{posLineIdx++;posCart.push({idx:posLineIdx,ref:p.ref||"",barcode:p.barcode||"",designation:p.designation,prix_unitaire:parseFloat(p.prix_vente)||0,quantite:q,taux_tva:parseFloat(p.tva_code)||18,product_id:p.id||null})}
  renderPosCart();
}

function addPosLineManual(){
  var desc=prompt("Designation de la ligne :");if(!desc||!desc.trim())return;
  var prix=parseFloat(prompt("Prix unitaire (FCFA) :"))||0;
  var q=parseInt(document.getElementById("pos-qte").value)||1;
  posLineIdx++;posCart.push({idx:posLineIdx,ref:"",barcode:"",designation:desc.trim(),prix_unitaire:prix,quantite:q,taux_tva:18,product_id:null});
  renderPosCart();
}

function scanBarcode(code){
  code=(code||"").trim();
  var bi=document.getElementById("pos-barcode");
  if(!code){bi.focus();return}
  fetch("/api/products/barcode?code="+encodeURIComponent(code)).then(function(r){return r.json()}).then(function(d){
    if(d.found){addToCart(d.product)}
    else{showToast("Code-barres inconnu : "+code,"err")}
    bi.value="";bi.focus();
  }).catch(function(){showToast("Erreur scan","err");bi.value="";bi.focus()});
}

function posInstantSearch(raw){
  raw=(raw||"").trim();
  clearTimeout(posSearchTimer);
  var grid=document.getElementById("pos-products-grid");
  var head=document.getElementById("pos-products-head");
  var results=document.getElementById("pos-results");
  if(raw.length===0){
    head.textContent="Meilleures ventes";
    grid.style.display="grid";
    results.innerHTML="";
    return;
  }
  if(raw.length===1){return;}
  grid.style.display="none";
  results.innerHTML='<div class="pos-results-empty">Recherche...</div>';
  posSearchTimer=setTimeout(function(){
    fetch("/api/pos/products/search?q="+encodeURIComponent(raw)).then(function(r){return r.json()}).then(function(list){
      if(!Array.isArray(list)||!list.length){head.textContent="Aucun resultat";results.innerHTML='<div class="pos-results-empty">Aucun article pour &laquo; '+_h(raw)+' &raquo;</div>';return}
      head.textContent=list.length+" article(s) trouve(s)";
      results.innerHTML="";
      list.forEach(function(p){results.appendChild(makeTileEl(p))});
    }).catch(function(){results.innerHTML='<div class="pos-results-empty">Erreur de recherche</div>'});
  },250);
}

function makeTileEl(p){
  var d=document.createElement("div");d.className="pos-tile";
  d.addEventListener("click",function(){addToCart(p)});
  var nom=document.createElement("div");nom.className="pos-tile-nom";nom.textContent=p.designation;
  var ref=document.createElement("div");ref.className="pos-tile-ref";ref.textContent=p.ref||"";
  var foot=document.createElement("div");foot.className="pos-tile-foot";
  var prix=document.createElement("span");prix.className="pos-tile-prix";prix.textContent=(Number(p.prix_vente)||0).toLocaleString("fr-FR");
  var stockVal=Number(p.stock_reel)||0;
  var st=document.createElement("span");st.className="pos-tile-stock"+(stockVal<=0?" out":stockVal<5?" low":"");st.textContent=stockVal.toLocaleString("fr-FR");
  foot.appendChild(prix);foot.appendChild(st);
  d.appendChild(nom);d.appendChild(ref);d.appendChild(foot);
  return d;
}

function renderPosCart(){
  var body="";var total=0;var nbArt=0;
  posCart.forEach(function(l){
    var ttc=round2(l.quantite*l.prix_unitaire*(1+l.taux_tva/100));
    total+=ttc;nbArt+=l.quantite;
    body+="<tr>"
      +"<td><div style='font-weight:700;font-size:12.5px'>"+_h(l.designation)+"</div><div style='font-size:10px;color:#94a3b8'>"+_h(l.ref||l.barcode||"")+"</div></td>"
      +"<td style='text-align:center'><div class='pos-qty-stepper'>"
      +"<button type='button' onclick='updatePosQte("+l.idx+","+(l.quantite-1)+")'>&minus;</button>"
      +"<input type='number' value='"+l.quantite+"' min='0.5' step='0.5' onchange='updatePosQte("+l.idx+",this.value)'>"
      +"<button type='button' onclick='updatePosQte("+l.idx+","+(l.quantite+1)+")'>+</button>"
      +"</div></td>"
      +"<td style='text-align:right;color:#64748b'>"+Number(l.prix_unitaire).toLocaleString("fr-FR")+"</td>"
      +"<td style='text-align:right;font-weight:800;color:#0f172a'>"+Number(ttc).toLocaleString("fr-FR")+"</td>"
      +"<td><button class='btn-sm btn-danger' onclick='removePosLine("+l.idx+")'>&times;</button></td></tr>";
  });
  document.getElementById("pos-cart-body").innerHTML=body;
  document.getElementById("pos-total").textContent=Number(total).toLocaleString("fr-FR")+" FCFA";
  document.getElementById("pos-articles-count").textContent=nbArt;
  document.getElementById("pos-cart-empty").style.display=posCart.length?"none":"block";
  calcMonnaie();
}

function updatePosQte(idx,val){var l=posCart.filter(function(x){return x.idx===idx})[0];if(l){l.quantite=parseFloat(val)||1;renderPosCart()}}
function removePosLine(idx){posCart=posCart.filter(function(l){return l.idx!==idx});renderPosCart()}
function clearCart(){posCart=[];posLineIdx=0;renderPosCart()}
function round2(n){return Math.round(n*100)/100}

function calcMonnaie(){
  var total=0;posCart.forEach(function(l){total+=round2(l.quantite*l.prix_unitaire*(1+l.taux_tva/100))});
  var recu=parseFloat(document.getElementById("pos-recu").value)||0;
  var mode=document.getElementById("pos-paiement").value;
  var monnaie=round2(recu-total);
  var el=document.getElementById("pos-monnaie");
  var btn=document.getElementById("pos-validate");
  if(posCart.length===0){el.className="pos-monnaie";el.textContent="";btn.disabled=true;btn.textContent="Ticket vide";return}
  if(mode==="especes"&&monnaie<0){
    el.className="pos-monnaie ko";el.textContent="Manque : "+Number(Math.abs(monnaie)).toLocaleString("fr-FR")+" FCFA";
    btn.disabled=true;btn.textContent="Manque "+Number(Math.abs(monnaie)).toLocaleString("fr-FR")+" FCFA";
  }else{
    el.className="pos-monnaie ok";
    el.textContent=mode==="especes"?"A rendre : "+Number(monnaie).toLocaleString("fr-FR")+" FCFA":"Total : "+Number(total).toLocaleString("fr-FR")+" FCFA";
    btn.disabled=false;btn.textContent="Valider & imprimer";
  }
}

function validerTicket(){
  if(posCart.length===0){showToast("Ticket vide","err");return}
  var lignes=[];
  posCart.forEach(function(l){lignes.push({designation:l.designation,quantite:l.quantite,prix_unitaire:l.prix_unitaire,taux_tva:l.taux_tva,code_article:l.ref,code_compte:"",famille:"",barcode:l.barcode||"",product_id:l.product_id||null})});
  var vid=document.getElementById("pos-vendeur").value;
  var pcd=getPosClientData(document.getElementById("pos-client").value);
  var data={tiers_nom:pcd.nom||"Client comptoir",
            tiers_code:pcd.code||"",
            contact_id:pcd.id||null,
            mode_paiement:document.getElementById("pos-paiement").value,
            montant_recu:parseFloat(document.getElementById("pos-recu").value)||0,
            vendeur_id:vid?parseInt(vid):null,lignes:lignes};
  var btn=document.getElementById("pos-validate");
  btn.disabled=true;btn.textContent="Validation...";
  api("POST","/api/pos/ticket",data).then(function(d){
    if(d.success){
      var msg="Ticket "+d.numero+" valide";
      if(d.invoice_numero){msg+=" - Facture "+d.invoice_numero}
      if(d.sfec_ok){msg+=" - SFEC certifiee ("+String(d.sfec_num_certif||"").slice(0,20)+")"}
      else if(d.sfec_error){msg+=" - SFEC: "+String(d.sfec_error).slice(0,40)}
      msg+=" - Monnaie : "+Number(d.monnaie_rendue||0).toLocaleString("fr-FR")+" FCFA";
      if(d.sage_ok){msg+=" - ecrite dans Sage"}
      showToast(msg,(d.sfec_ok&&d.sage_ok)?"ok":"warn");
      clearCart();
      document.getElementById("pos-recu").value=0;
      document.getElementById("pos-client").selectedIndex=0;
      setTimeout(function(){location.href="/pos/ticket/"+d.id+"/print"},1400);
    }else{
      btn.disabled=false;btn.textContent="Valider & imprimer";
      showToast("Erreur : "+d.error,"err");
    }
  }).catch(function(e){btn.disabled=false;btn.textContent="Valider & imprimer";showToast("Erreur : "+e,"err")});
}

function syncPosArticles(){api("POST","/api/articles/sync",{}).then(function(d){alert(d.message||"Sync OK");setTimeout(function(){location.reload()},2500)}).catch(function(e){alert("Erreur : "+e)})}
window.STOCK_CONTROL = @@STOCK_CTL@@;
function refreshStockBtn(){var b=document.getElementById("btn-stock");if(!b)return;b.textContent="Stock: "+(window.STOCK_CONTROL?"ON":"OFF");b.classList.toggle("btn-success",!!window.STOCK_CONTROL);b.classList.toggle("btn-warning",!window.STOCK_CONTROL);}
function toggleStock(){api("POST","/api/pos/config",{stock_control:!window.STOCK_CONTROL}).then(function(d){if(d.success){window.STOCK_CONTROL=!!d.stock_control;refreshStockBtn();showToast("Controle de stock "+(window.STOCK_CONTROL?"ACTIVE":"DESACTIVE"),"ok")}else{showToast(d.error||"Erreur","err")}});}
refreshStockBtn();

var POS_PAY_METHODS=[
  {v:"especes",l:"Especes",i:'<path d="M12 1v22"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>'},
  {v:"mobile_money",l:"Mobile",i:'<rect x="7" y="2" width="10" height="20" rx="2"/><line x1="11" y1="18" x2="13" y2="18"/>'},
  {v:"carte",l:"Carte",i:'<rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/>'},
  {v:"virement",l:"Virement",i:'<polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/>'},
  {v:"cheque",l:"Cheque",i:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>'},
  {v:"mixte",l:"Mixte",i:'<circle cx="9" cy="9" r="7"/><path d="M14.5 4.5A7 7 0 1 1 4.5 14.5"/>'}
];
function renderPayMethods(){
  var wrap=document.getElementById("pos-pay-methods");if(!wrap)return;
  var current=document.getElementById("pos-paiement").value||"especes";
  wrap.innerHTML="";
  POS_PAY_METHODS.forEach(function(m){
    var b=document.createElement("button");
    b.type="button";b.className="pos-pay-btn"+(m.v===current?" active":"");
    b.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+m.i+'</svg><span>'+m.l+'</span>';
    b.addEventListener("click",function(){setPayMethod(m.v)});
    wrap.appendChild(b);
  });
}
function setPayMethod(v){
  document.getElementById("pos-paiement").value=v;
  syncPayUI();
  calcMonnaie();
}
function syncPayUI(){
  var current=document.getElementById("pos-paiement").value;
  var wrap=document.getElementById("pos-pay-methods");if(!wrap)return;
  Array.prototype.forEach.call(wrap.children,function(btn,i){btn.classList.toggle("active",POS_PAY_METHODS[i].v===current)});
}
renderPayMethods();
</script>""".replace("@@STATS@@", stats_html) \
        .replace("@@TILES@@", top10_tiles) \
        .replace("@@VENDEURS@@", vendeur_opts_html) \
        .replace("@@POS_CLIENTS@@", pos_clients_html) \
        .replace("@@POS_CLIENTS_JS@@", pos_clients_js) \
        .replace("@@TICKETS@@", ticket_rows_html) \
        .replace("@@STOCK_CTL@@", "true" if pos_engine.stock_control_enabled() else "false")
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
            import invoice_engine
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

    body = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>Ticket {numero}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Courier New',monospace;color:#000;background:#fff;max-width:80mm;margin:0 auto;font-size:12px}}
.header{{text-align:center;margin-bottom:8px}}
.logo{{margin-bottom:6px}}
.company{{font-size:15px;font-weight:bold;text-transform:uppercase;letter-spacing:1px}}
.info{{font-size:10px;color:#000}}
.rule{{border-top:1px dashed #000;margin:6px 0}}
.ticket-info{{padding:2px 0}}
.ticket-info div{{display:flex;justify-content:space-between;padding:1px 0;font-size:11px}}
table{{width:100%;border-collapse:collapse;margin:4px 0}}
th,td{{padding:2px 0;font-size:11px;border-bottom:1px dotted #ccc}}
th{{text-align:left;font-size:10px;text-transform:uppercase}}
td.r{{text-align:right}}
.total{{border-top:2px solid #000;font-weight:bold;font-size:15px;padding-top:6px;margin-top:4px}}
.footer{{text-align:center;margin-top:10px;font-size:10px}}
.pay-row{{display:flex;justify-content:space-between;padding:1px 0;font-size:12px}}
.pay-row.big{{font-weight:bold;font-size:14px;margin-top:4px}}
.no-print{{margin-top:12px;text-align:center}}
.no-print a,.no-print button{{display:inline-block;margin:4px;padding:8px 14px;font-size:12px;text-decoration:none;border:none;border-radius:4px;cursor:pointer;font-family:inherit}}
@media print{{body{{width:80mm}} .no-print{{display:none!important}}}}
</style></head><body onload="setTimeout(function(){{window.print()}},500)">
<div class="header">
<div class="logo">{logo}</div>
<div class="company">{company_name}</div>
{company_meta}
</div>
<div class="rule"></div>
<div class="ticket-info">
<div><span><b>TICKET DE VENTE</b></span><span>{date}</span></div>
<div><span>N&deg; <b>{numero}</b></span><span>{caissier}</span></div>
<div><span>Client</span><span>{client}</span></div>
{invoice_html}
</div>
<div class="rule"></div>
<table>
<thead><tr><th>Article</th><th class="r" style="text-align:right">Qte</th><th class="r" style="text-align:right">PU</th><th class="r" style="text-align:right">Total</th></tr></thead>
<tbody>{lignes}</tbody>
</table>
<div class="rule"></div>
<div class="pay-row big"><span>TOTAL TTC</span><span>{total:,.0f} FCFA</span></div>
<div class="rule"></div>
<div class="pay-row"><span>Mode</span><span>{paiement}</span></div>
<div class="pay-row"><span>Recu</span><span>{recu:,.0f} FCFA</span></div>
<div class="pay-row big"><span>Monnaie rendue</span><span>{monnaie:,.0f} FCFA</span></div>
{sfec}
<div class="footer">
{message}
</div>
<div class="no-print">
<button onclick="window.print()">Imprimer</button>
<a href="/api/pos/ticket/{ticket_id}/pdf" class="no-print" target="_blank">PDF</a>
<a href="/pos">Retour caisse</a>
</div>
</body></html>""".format(
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
        total=ticket.get("montant_ttc", 0),
        paiement=_esc(paiement_label),
        recu=ticket.get("montant_recu", 0),
        monnaie=ticket.get("monnaie_rendue", 0),
        message=get_setting("ticket_message", "Merci pour votre achat !"),
        ticket_id=ticket["id"]
    )
    return body


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

    body = """
<div class="card"><h2>Historique des Ventes</h2>
<div class="stat-grid" style="margin-bottom:16px">
<div class="stat"><div class="value">{total}</div><div class="label">Total ventes</div></div>
<div class="stat"><div class="value">{tickets_jour}</div><div class="label">Aujourd'hui</div></div>
<div class="stat"><div class="value">{ca_jour:,.0f}</div><div class="label">CA Jour (FCFA)</div></div>
<div class="stat"><div class="value">{ca_total:,.0f}</div><div class="label">CA Total (FCFA)</div></div>
</div></div>
<div class="card">
<h2>Filtres</h2>
<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:end">
<div><label>Date debut</label><input type="date" id="f-date-from"></div>
<div><label>Date fin</label><input type="date" id="f-date-to"></div>
<div><label>Vendeur</label><select id="f-vendeur">{vendeur_opts}</select></div>
<div><label>Recherche</label><input type="text" id="f-search" placeholder="N client..."></div>
<div><button class="btn btn-primary" onclick="filterSales()">Filtrer</button></div>
</div></div>
<div class="card"><h2>Ventes ({count})</h2>
<table><thead><tr><th>Numero</th><th>Date</th><th>Client</th><th style="text-align:right">Montant</th><th>Paiement</th><th>Vendeur</th><th>SFEC</th><th></th></tr></thead>
<tbody id="sales-body">{rows}</tbody></table></div>
<script>
function certifySales(invoiceId){{
  if(!invoiceId)return;
  api("POST","/api/sfec/certify",{{invoice_id:"INV-"+invoiceId}}).then(function(r){{
    if(r.success&&r.certification_number){{showToast("Certifiee SFEC: "+r.certification_number,"ok");setTimeout(function(){{location.reload()}},1800)}}
    else if(r.success){{showToast("Certification envoyee, en attente de finalisation...","warn")}}
    else{{showToast("Certification: "+(r.error||"echec"),"err")}}
  }})
}}
function sfecCell(t){{
  var ss=t.sfec_statut||"";
  if(ss==="CERTIFIE"||ss==="DEJA_CERTIFIE")return '<span class="badge badge-ok" title="'+(t.sfec_num_certif||"").slice(0,25)+'">Certifiee</span>';
  var ic=t.invoice_id;
  if(ss==="EN_COURS")return '<span class="badge badge-warn">En attente</span>'+(ic?' <button class="btn btn-sm" onclick="certifySales('+ic+')">Certifier</button>':"");
  if(ss==="ERREUR")return '<span class="badge badge-err">Erreur</span>'+(ic?' <button class="btn btn-sm" onclick="certifySales('+ic+')">Certifier</button>':"");
  if(ic)return '<span class="badge badge-info">Non certifiee</span> <button class="btn btn-sm" onclick="certifySales('+ic+')">Certifier</button>';
  return "-";
}}
function filterSales(){{
  var df=document.getElementById("f-date-from").value;
  var dt=document.getElementById("f-date-to").value;
  var vid=document.getElementById("f-vendeur").value;
  var q=document.getElementById("f-search").value;
  var url="/api/pos/tickets?limit=200";
  if(df)url+="&date_from="+df;
  if(dt)url+="&date_to="+dt;
  if(vid)url+="&vendeur_id="+vid;
  if(q)url+="&search="+encodeURIComponent(q);
  api("GET",url).then(function(d){{
    var html="";
    (d.tickets||[]).forEach(function(t){{
      var v=(t.vendeur_prenom||"")+" "+(t.vendeur_nom||"");
      html+="<tr><td>"+t.numero+"</td><td>"+(t.date_ticket||"").substring(0,16)+"</td><td>"+t.tiers_nom+"</td><td style='text-align:right'>"+Number(t.montant_ttc).toLocaleString()+"</td><td>"+t.mode_paiement+"</td><td>"+(v.trim()||"-")+"</td><td>"+sfecCell(t)+"</td><td><a href='/pos/ticket/"+t.id+"/print' class='btn btn-sm' target='_blank'>Voir</a></td></tr>"
    }});
    if(!html)html='<tr><td colspan="8" style="text-align:center;color:#64748b">Aucune vente</td></tr>';
    document.getElementById("sales-body").innerHTML=html;
  }})
}}
</script></div>""".format(
        total=stats.get("total", 0), tickets_jour=stats.get("tickets_jour", 0),
        ca_jour=stats.get("ca_jour", 0), ca_total=stats.get("ca_total", 0),
        count=result.get("total", 0), rows=ticket_rows,
        vendeur_opts=vendeur_options
    )
    return _page(body)


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

    body = """
<div class="card"><h2>Equipe Vente</h2>
<div class="stat-grid" style="margin-bottom:16px">
<div class="stat"><div class="value">{total}</div><div class="label">Vendeurs</div></div>
<div class="stat"><div class="value">{actifs}</div><div class="label">Actifs</div></div>
<div class="stat"><div class="value">{ca_jour:,.0f}</div><div class="label">CA Jour equipe</div></div>
</div>
<button class="btn btn-primary" onclick="showNewVendeur()">+ Nouveau Vendeur</button>
</div>
<div class="card"><h2>Liste des Vendeurs</h2>
<table><thead><tr><th>Code</th><th>Nom</th><th>Role</th><th>Contact</th><th>Ventes</th><th style='text-align:right'>CA</th><th>Statut</th><th></th></tr></thead>
<tbody>{rows}</tbody></table></div>

<div id="modal-vendeur" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:100;display:none;align-items:center;justify-content:center">
<div class="card" style="width:420px;max-width:95vw">
<h2 id="modal-title">Nouveau Vendeur</h2>
<form id="form-vendeur">
<input type="hidden" id="v-id">
<div class="grid-2">
<div><label>Code</label><input type="text" id="v-code" required></div>
<div><label>Nom</label><input type="text" id="v-nom" required></div>
<div><label>Prenom</label><input type="text" id="v-prenom"></div>
<div><label>Role</label><select id="v-role"><option value="vendeur">Vendeur</option><option value="vendeuse">Vendeuse</option><option value="caissier">Caissier</option><option value="responsable">Responsable</option></select></div>
<div><label>Email</label><input type="email" id="v-email"></div>
<div><label>Telephone</label><input type="text" id="v-tel"></div>
</div>
<div style="margin-top:16px">
<button type="submit" class="btn btn-primary">Enregistrer</button>
<button type="button" class="btn" onclick="closeModal()" style="margin-left:8px">Annuler</button>
</div></form></div></div>
<script>
function showNewVendeur(){{document.getElementById("modal-title").textContent="Nouveau Vendeur";document.getElementById("v-id").value="";document.getElementById("v-code").value="";document.getElementById("v-nom").value="";document.getElementById("v-prenom").value="";document.getElementById("v-role").value="vendeur";document.getElementById("v-email").value="";document.getElementById("v-tel").value="";document.getElementById("modal-vendeur").style.display="flex"}}
function editVendeur(id){{api("GET","/api/vendeurs/"+id).then(function(v){{document.getElementById("modal-title").textContent="Modifier Vendeur";document.getElementById("v-id").value=v.id;document.getElementById("v-code").value=v.code;document.getElementById("v-nom").value=v.nom;document.getElementById("v-prenom").value=v.prenom||"";document.getElementById("v-role").value=v.role||"vendeur";document.getElementById("v-email").value=v.email||"";document.getElementById("v-tel").value=v.telephone||"";document.getElementById("modal-vendeur").style.display="flex"}})}}
function closeModal(){{document.getElementById("modal-vendeur").style.display="none"}}
document.getElementById("form-vendeur").onsubmit=function(e){{e.preventDefault();var id=document.getElementById("v-id").value;var d={{code:document.getElementById("v-code").value,nom:document.getElementById("v-nom").value,prenom:document.getElementById("v-prenom").value,role:document.getElementById("v-role").value,email:document.getElementById("v-email").value,telephone:document.getElementById("v-tel").value}};var url=id?"/api/vendeurs/"+id:"/api/vendeurs";var method=id?"PUT":"POST";api(method,url,d).then(function(r){{if(r.success||r.id){{showToast("Vendeur sauvegarde","ok");setTimeout(function(){{location.reload()}},1000)}}else{{showToast("Erreur: "+(r.error||"inconnue"),"err")}}}})}}
</script>""".format(
        total=len(vendeurs),
        actifs=sum(1 for v in vendeurs if v.get("est_actif")),
        ca_jour=sum(s.get("ca_total", 0) for s in stats),
        rows=rows
    )
    return _page(body)


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

    html = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gestion Clients - T-CONNECTOR</title><style>{css}</style></head><body>
{nav}<div class="main-content">
<h2 style="margin-bottom:16px">Gestion Clients &amp; Fournisseurs</h2>

<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <h2 style="margin:0">Contacts ({total})</h2>
    <button class="btn btn-primary" onclick="showModal()">+ Nouveau Client</button>
  </div>
  <div style="display:flex;gap:12px;margin-bottom:12px">
    <div class="stat" style="flex:1"><div class="value">{nb_clients}</div><div class="label">Clients</div></div>
    <div class="stat" style="flex:1"><div class="value">{nb_fournis}</div><div class="label">Fournisseurs</div></div>
    <div class="stat" style="flex:1"><div class="value">{nb_total}</div><div class="label">Total</div></div>
  </div>
  <div style="margin-bottom:12px">
    <input type="text" id="searchContact" placeholder="Rechercher un contact..." oninput="filterContacts()" style="max-width:400px">
  </div>
  <table>
    <thead><tr><th>Code</th><th>Type</th><th>Nom</th><th>NIU</th><th>Email</th><th>Telephone</th><th>Ville</th><th>Statut</th><th>Actions</th></tr></thead>
    <tbody id="contactsBody">{rows}</tbody>
  </table>
</div>

<div id="contactModal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.7);z-index:1000;align-items:center;justify-content:center">
  <div class="card" style="width:500px;max-width:95vw">
    <h2 id="modalTitle">Nouveau Client</h2>
    <input type="hidden" id="editId">
    <div class="grid-2">
      <div><label>Code</label><input type="text" id="fCode" placeholder="CLI-XXX"></div>
      <div><label>Type</label><select id="fType"><option value="client">Client</option><option value="fournisseur">Fournisseur</option></select></div>
    </div>
    <label>Nom / Raison Sociale *</label><input type="text" id="fNom" placeholder="Nom complet">
    <div class="grid-2">
      <div><label>NIU (Identifiant fiscal)</label><input type="text" id="fNiu" placeholder="CG123456789"></div>
      <div><label>Ville</label><input type="text" id="fVille" placeholder="Pointe-Noire"></div>
    </div>
    <div class="grid-2">
      <div><label>Email</label><input type="email" id="fEmail" placeholder="contact@societe.cg"></div>
      <div><label>Telephone</label><input type="text" id="fTel" placeholder="+24206XXXXXXX"></div>
    </div>
    <label>Adresse</label><input type="text" id="fAdresse" placeholder="Quartier, rue...">
    <div style="display:flex;gap:8px;margin-top:16px;justify-content:flex-end">
      <button class="btn" style="background:#334155;color:#94a3b8" onclick="hideModal()">Annuler</button>
      <button class="btn btn-primary" onclick="saveContact()">Enregistrer</button>
    </div>
  </div>
</div>

{toast}
<script>
window.CSRF_TOKEN = {csrf_json};
var contacts = {contacts_json};

function filterContacts() {{
  var q = document.getElementById("searchContact").value.toLowerCase();
  var rows = document.getElementById("contactsBody").querySelectorAll("tr");
  for(var i=0;i<rows.length;i++){{
    var txt = rows[i].textContent.toLowerCase();
    rows[i].style.display = txt.indexOf(q)>=0 ? "" : "none";
  }}
}}

function showModal(contact) {{
  var m = document.getElementById("contactModal");
  m.style.display = "flex";
  if(contact) {{
    document.getElementById("modalTitle").textContent = "Editer Contact";
    document.getElementById("editId").value = contact.id;
    document.getElementById("fCode").value = contact.code || "";
    document.getElementById("fType").value = contact.type || "client";
    document.getElementById("fNom").value = contact.nom || "";
    document.getElementById("fNiu").value = contact.niu || "";
    document.getElementById("fVille").value = contact.ville || "";
    document.getElementById("fEmail").value = contact.email || "";
    document.getElementById("fTel").value = contact.telephone || "";
    document.getElementById("fAdresse").value = contact.adresse || "";
  }} else {{
    document.getElementById("modalTitle").textContent = "Nouveau Client";
    document.getElementById("editId").value = "";
    document.getElementById("fCode").value = "";
    document.getElementById("fNom").value = "";
    document.getElementById("fNiu").value = "";
    document.getElementById("fVille").value = "";
    document.getElementById("fEmail").value = "";
    document.getElementById("fTel").value = "";
    document.getElementById("fAdresse").value = "";
  }}
}}
function hideModal() {{ document.getElementById("contactModal").style.display = "none"; }}

function editContact(id) {{
  for(var i=0;i<contacts.length;i++){{
    if(contacts[i].id === id) {{ showModal(contacts[i]); return; }}
  }}
}}

function deleteContact(id) {{
  if(!confirm("Supprimer ce contact ?")) return;
  api("DELETE", "/api/contacts/" + id)
    .then(function(d){{
      if(d.success) {{ showToast("Contact supprime","ok"); setTimeout(function(){{location.reload()}},800); }}
      else showToast(d.error || "Erreur","err");
    }});
}}

function saveContact() {{
  var editId = document.getElementById("editId").value;
  var data = {{
    code: document.getElementById("fCode").value,
    type: document.getElementById("fType").value,
    nom: document.getElementById("fNom").value,
    niu: document.getElementById("fNiu").value,
    ville: document.getElementById("fVille").value,
    email: document.getElementById("fEmail").value,
    telephone: document.getElementById("fTel").value,
    adresse: document.getElementById("fAdresse").value
  }};
  if(!data.nom) {{ showToast("Nom requis","err"); return; }}
  var url = "/api/contacts";
  var method = "POST";
  if(editId) {{ url = "/api/contacts/" + editId; method = "PUT"; }}
  api(method, url, data)
    .then(function(d){{
      if(d.success) {{ showToast(editId ? "Contact modifie" : "Contact cree","ok"); setTimeout(function(){{location.reload()}},800); }}
      else showToast(d.error || "Erreur","err");
    }});
}}
</script>
</div></body></html>""".format(
        css=CSS, nav=_sidebar(request.path),
        total=len(contacts), nb_clients=client_count, nb_fournis=fourni_count, nb_total=len(contacts),
        rows=rows,
        contacts_json=__import__("json").dumps(contacts),
        csrf_json=json.dumps(_get_csrf_token()),
        toast=ALERT_ZONE
    )
    return render_template_string(html)


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

    body = """
<div class="card"><h2>Comptes utilisateurs</h2>
<div class="stat-grid" style="margin-bottom:16px">
<div class="stat"><div class="value">@NB@</div><div class="label">Comptes</div></div>
<div class="stat"><div class="value">@ACTIV@</div><div class="label">Actifs</div></div>
<div class="stat"><div class="value">@ADMINS@</div><div class="label">Administrateurs</div></div>
</div>
<button class="btn btn-primary" onclick="showNewUser()">+ Nouveau compte</button>
</div>
<div class="card"><h2>Liste des comptes</h2>
<table><thead><tr><th>Nom</th><th>Email</th><th>Role</th><th>Statut</th><th>Derniere connexion</th><th>Actions</th></tr></thead>
<tbody>{rows}</tbody></table>
<div style="font-size:12px;color:#64748b;margin-top:8px">Provider d'authentification : <b>@PROVIDER@</b> (sage = comptes verifies depuis la base Sage si elle expose des utilisateurs)</div>
</div>
<div class="card"><h2>Droits d'acces par role</h2>
@MATRIX@
<div style="margin-top:12px"><button class="btn btn-primary" onclick="saveMatrix()">Enregistrer les droits</button>
<span id="matrix-status" style="margin-left:8px;font-size:12px;color:#64748b"></span></div>
</div>
<div class="card"><h2>Securite de la session</h2>
<div style="display:flex;gap:14px;flex-wrap:wrap;align-items:end">
<div><label>Coiffre (provider)</label><select id="sec-provider"><option value="local">Local</option><option value="sage">Sage</option></select></div>
<div><label>Tentatives max</label><input type="number" id="sec-attempts" min="1" max="20" style="width:110px"></div>
<div><label>Verrouillage (min)</label><input type="number" id="sec-lock" min="1" max="1440" style="width:110px"></div>
<div><label>Session max (heures)</label><input type="number" id="sec-hours" min="1" max="720" style="width:110px"></div>
<div><label>Inactivite (min, 0=off)</label><input type="number" id="sec-idle" min="0" max="1440" style="width:110px"></div>
<div><label><input type="checkbox" id="sec-https"> HTTPS / Secure cookie</label></div>
<div><button class="btn btn-primary" onclick="saveSec()">Enregistrer</button></div>
</div>
<div style="font-size:12px;color:#64748b;margin-top:8px">Un compte est verrouille apres trop d'echecs. Redemarrez le dashboard pour appliquer <b>https_only</b>.</div>
</div>
<div class="card no-print"><h2>Journal d'audit</h2>
<table><thead><tr><th>Heure</th><th>Compte</th><th>Action</th><th>Detail</th><th>IP</th></tr></thead>
<tbody>@AUDIT@</tbody></table></div>

<div id="user-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:100;align-items:center;justify-content:center">
<div class="card" style="width:440px;max-width:95vw">
<h2 id="user-modal-title">Nouveau compte</h2>
<form id="user-form">
<input type="hidden" id="u-id">
<div class="grid-2">
<div><label>Nom</label><input type="text" id="u-nom"></div>
<div><label>Prenom</label><input type="text" id="u-prenom"></div>
</div>
<div><label>Email *</label><input type="email" id="u-email" required></div>
<div class="grid-2">
<div><label>Role</label><select id="u-role">@ROLEOPTS@</select></div>
<div><label>Actif</label><select id="u-actif"><option value="1">Oui</option><option value="0">Non</option></select></div>
</div>
<div><label id="u-pwd-label">Mot de passe provisoire * (min 8 char.)</label><input type="password" id="u-pwd"></div>
<div style="display:flex;gap:8px;margin-top:16px;justify-content:flex-end">
<button type="button" class="btn btn-sm btn-ghost" onclick="hideUserModal()">Annuler</button>
<button type="submit" class="btn btn-primary">Enregistrer</button>
</div>
</form>
</div>
</div>

<script>
function visible(){var ids=["u-id","u-nom","u-prenom","u-email","u-role","u-actif"].concat(["u-pwd"]);for(var i=0;i<ids.length;i++){var el=document.getElementById(ids[i]);if(el)el.value="";}document.getElementById("u-pwd").style.display="";document.getElementById("u-pwd").required=true;}
function showNewUser(){
visible();
document.getElementById("u-actif").value="1";
document.getElementById("u-pwd-label").textContent="Mot de passe initial * (min 8 char.)";
document.getElementById("user-modal-title").textContent="Nouveau compte";
document.getElementById("user-modal").style.display="flex";
document.getElementById("u-email").focus();
}
function hideUserModal(){document.getElementById("user-modal").style.display="none";}
function editUser(id){
api("GET","/api/utilisateurs").then(function(d){
var u=null;for(var i=0;i<d.comptes.length;i++){if(d.comptes[i].id===id){u=d.comptes[i];break;}}
if(!u)return;
visible();
document.getElementById("u-id").value=u.id;
document.getElementById("u-nom").value=u.nom||"";
document.getElementById("u-prenom").value=u.prenom||"";
document.getElementById("u-email").value=u.email;
document.getElementById("u-role").value=u.role;
document.getElementById("u-actif").value=u.est_actif?"1":"0";
document.getElementById("u-pwd").required=false;
document.getElementById("u-pwd").style.display="none";
document.getElementById("u-pwd-label").textContent="Laisser vide = ne pas changer le mot de passe";
document.getElementById("user-modal-title").textContent="Modifier le compte";
document.getElementById("user-modal").style.display="flex";
});
}
document.getElementById("user-form").onsubmit=function(e){
e.preventDefault();
var id=document.getElementById("u-id").value;
var d={
nom:document.getElementById("u-nom").value,
prenom:document.getElementById("u-prenom").value,
email:document.getElementById("u-email").value,
role:document.getElementById("u-role").value,
est_actif:document.getElementById("u-actif").value==="1",
password:document.getElementById("u-pwd").value
};
var url=id?"/api/utilisateurs/"+id:"/api/utilisateurs";
var m=id?"PUT":"POST";
api(m,url,d).then(function(r){
if(r.success==false){showToast(r.error||"Erreur","err");return;}
showToast(id?"Compte modifie":"Compte cree","ok");
setTimeout(function(){location.reload()},900);
});
};
function resetPwd(id){
var p=prompt("Nouveau mot de passe (min 8 caracteres) :");
if(!p)return;
if(p.length<8){showToast("Minimum 8 caracteres","err");return;}
api("POST","/api/utilisateurs/"+id+"/password",{password:p}).then(function(r){
if(r.success==false){showToast(r.error||"Erreur","err");return;}
showToast("Mot de passe reinitialise","ok");
});
}
function toggleActif(id){
api("PUT","/api/utilisateurs/"+id,{toggle_actif:true}).then(function(r){
if(r.success==false){showToast(r.error||"Erreur","err");return;}
showToast("Statut mis a jour","ok");setTimeout(function(){location.reload()},700);
});
}
function delUser(id){
if(!confirm("Supprimer ce compte ?"))return;
api("DELETE","/api/utilisateurs/"+id).then(function(r){
if(r.success==false){showToast(r.error||"Erreur","err");return;}
showToast("Compte supprime","ok");setTimeout(function(){location.reload()},700);
});
}
function saveMatrix(){
var matrix={};
["admin","responsable","caissiere","financiere"].forEach(function(r){matrix[r]=[];});
document.querySelectorAll("#matrix input[type=checkbox]").forEach(function(cb){
if(cb.checked)matrix[cb.dataset.role].push(cb.dataset.perm);
});
api("POST","/api/auth/permissions",{matrix:matrix}).then(function(r){
if(r.success==false){showToast(r.error||"Erreur","err");return;}
document.getElementById("matrix-status").textContent="Droits enregistres";
setTimeout(function(){document.getElementById("matrix-status").textContent="";},2500);
});
}
function secFill(c){
document.getElementById("sec-provider").value=c.provider||"local";
document.getElementById("sec-attempts").value=c.max_attempts||5;
document.getElementById("sec-lock").value=c.lock_minutes||15;
document.getElementById("sec-hours").value=c.session_max_age_hours||24;
document.getElementById("sec-idle").value=c.session_idle_minutes||60;
document.getElementById("sec-https").checked=!!c.https_only;
}
function saveSec(){
var d={
provider:document.getElementById("sec-provider").value,
max_attempts:Number(document.getElementById("sec-attempts").value)||5,
lock_minutes:Number(document.getElementById("sec-lock").value)||15,
session_max_age_hours:Number(document.getElementById("sec-hours").value)||24,
session_idle_minutes:Number(document.getElementById("sec-idle").value)||0,
https_only:document.getElementById("sec-https").checked
};
api("POST","/api/auth/config",d).then(function(r){
if(r.success==false){showToast(r.error||"Erreur","err");return;}
showToast("Securite enregistree","ok");
});
}
api("GET","/api/auth/config").then(function(d){if(d.config)secFill(d.config);});
</script>"""
    out = body
    out = out.replace("@NB@", str(len(comptes)))
    out = out.replace("@ACTIV@", str(sum(1 for u in comptes if u["est_actif"])))
    out = out.replace("@ADMINS@", str(sum(1 for u in comptes if u["role"] == "admin" and u["est_actif"])))
    out = out.replace("@ROWS@", _user_rows(comptes, me.get("user_id")))
    out = out.replace("@MATRIX@", _matrix_editor(matrix))
    out = out.replace("@AUDIT@", _audit_rows(audit))
    out = out.replace("@PROVIDER@", acfg.get("provider", "local"))
    out = out.replace("@ROLEOPTS@", role_opts)
    return _page(out)


@app.route("/compte/mot-de-passe")
@_login_required
def compte_password_page():
    body = """
<div class="card" style="max-width:460px"><h2>Changer mon mot de passe</h2>
<form id="pwd-form">
<div><label>Mot de passe actuel</label><input type="password" id="p-current" required></div>
<div><label>Nouveau mot de passe</label><input type="password" id="p-new" required minlength="8"></div>
<div><label>Confirmation</label><input type="password" id="p-confirm" required></div>
<div style="margin-top:16px"><button type="submit" class="btn btn-primary">Mettre a jour</button></div>
</form>
<p style="font-size:12px;color:#64748b;margin-top:12px">Minimum 8 caracteres. Le mot de passe est hache (PBKDF2) et n'est jamais stocke en clair.</p>
</div>
<script>
document.getElementById("pwd-form").onsubmit=function(e){
e.preventDefault();
var cur=document.getElementById("p-current").value;
var nv=document.getElementById("p-new").value;
var cf=document.getElementById("p-confirm").value;
if(nv!==cf){showToast("Les mots de passe ne correspondent pas","err");return;}
api("POST","/api/compte/password",{current:cur,nouveau:nv}).then(function(r){
if(r.success==false){showToast(r.error||"Erreur","err");return;}
showToast("Mot de passe mis a jour","ok");document.getElementById("pwd-form").reset();
});
};
</script>"""
    return _page(body)


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

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


# ══════════════════════════════════════════════════════════════
# ÉTAPE A3 (Splitter) — dashboard.py allégé.
# Les handlers de routes sont déplacés mécaniquement, SANS modification de
# logique, dans app/web/parking/<domaine>.py. On garde ici : l'instance
# `app`, toute la sécurité (before/after_request, CSRF, _login_required,
# permissions). Les routes sont enregistrées en bas via
# app.register_blueprint(...) (blueprints dans app/web/routes/<domaine>.py)
# avec exactement les mêmes URL / méthodes HTTP, et le wrapper
# @_login_required reproduit à l'identique dans chaque blueprint.
#
# NOTE : `from app.web.parking.common import _esc` se trouve en BAS de ce
# fichier (section enregistrement) — l'import en tête créerait un cycle
# (common.py lie _current_identity/_get_csrf_token depuis ce module).
# _esc n'est utilisé qu'à l'exécution (dans _deny / _auth_before_request).
# ══════════════════════════════════════════════════════════════



logger = logging.getLogger("t-connector.dashboard")

app = Flask(__name__)


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
            return redirect(url_for("auth.login_page"))
        try:
            live = user_auth.get_user(identity["user_id"])
        except Exception:
            live = None
        if not live or not live.get("est_actif"):
            session.clear()
            return redirect(url_for("auth.login_page"))
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
            return redirect(url_for("auth.login_page"))
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
            return redirect(url_for("auth.login_page"))
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
        return redirect(url_for("auth.login_page"))
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


# ══════════════════════════════════════════════════════════════
# ENREGISTREMENT DES BLUEPRINTS — handlers dans app/web/routes/ (A4/A5/A6)
# ══════════════════════════════════════════════════════════════

from app.web.parking.common import _esc  # helper partagé (utilisé à l'exécution)

from app.web.routes import billing as _rt_billing
from app.web.routes import pos as _rt_pos
from app.web.routes import directory as _rt_directory
from app.web.routes import auth as _rt_auth
from app.web.routes import dashboard as _rt_dashboard_pages
from app.web.routes import sync_api as _rt_sync_api
from app.web.routes import config as _rt_config

app.register_blueprint(_rt_config.bp)  # A5 : domaine configuration (/config, /api/config*, /api/db, /api/sfec, /api/tables, /api/tax-rates)

app.register_blueprint(_rt_auth.bp)  # A4 : auth (/login, /logout, /compte/mot-de-passe, /api/compte/password)
app.register_blueprint(_rt_dashboard_pages.bp)  # A4 : dashboard (/ /invoices /pending /certified /certified/<id>/print /sales /health /ready)
app.register_blueprint(_rt_sync_api.bp)  # A4 : sync_api (/api/sync*, /api/connectivity, /api/metrics, /api/certified*, /api/ledger-accounts, /api/retry-queue, /api/auth*, /api/audit, /api/csrf)


app.register_blueprint(_rt_billing.bp)    # A6 : facturation (/billing*, /api/invoices*)
app.register_blueprint(_rt_pos.bp)        # A6 : point de vente (/pos*, /api/products*, /api/pos*)
app.register_blueprint(_rt_directory.bp)  # A6 : annuaire (/clients, /vendeurs, /utilisateurs, /api/contacts*, /api/vendeurs*, /api/utilisateurs*)

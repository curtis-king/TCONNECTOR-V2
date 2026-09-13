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
# permissions). Les 93 routes sont enregistrées en bas via app.add_url_rule
# avec exactement les mêmes URL / endpoints / méthodes HTTP, et le wrapper
# @_login_required reproduit à l'identique.
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


# ══════════════════════════════════════════════════════════════
# ENREGISTREMENT DES ROUTES — handlers dans app/web/parking/ (A3)
# ══════════════════════════════════════════════════════════════

from app.web.parking.common import _esc  # helper partagé (utilisé à l'exécution)

from app.web.parking import auth as _pk_auth
from app.web.parking import dashboard_pages as _pk_dashboard_pages
from app.web.parking import config as _pk_config
from app.web.parking import billing as _pk_billing
from app.web.parking import pos as _pk_pos
from app.web.parking import directory as _pk_directory
from app.web.parking import sync_api as _pk_sync_api


def _add_route(rule, endpoint, view_func, methods, login_required):
    """Enregistre une route déplacée ; reproduit le décorateur @_login_required."""
    if login_required:
        view_func = _login_required(view_func)
    app.add_url_rule(rule, endpoint, view_func, methods=methods)


# ── AUTHENTIFICATION ──
_add_route("/login", "login_page", _pk_auth.login_page, ["GET", "POST"], False)
_add_route("/logout", "logout", _pk_auth.logout, ["GET"], False)

# ── PAGES TABLEAU DE BORD ──
_add_route("/", "index", _pk_dashboard_pages.index, ["GET"], True)
_add_route("/invoices", "invoices_page", _pk_dashboard_pages.invoices_page, ["GET"], True)
_add_route("/pending", "pending_page", _pk_dashboard_pages.pending_page, ["GET"], True)
_add_route("/certified", "certified_page", _pk_dashboard_pages.certified_page, ["GET"], True)
_add_route("/certified/<invoice_id>/print", "print_certified", _pk_dashboard_pages.print_certified, ["GET"], True)

# ── CONFIGURATION ──
_add_route("/config", "config_page", _pk_config.config_page, ["GET"], True)

# ── API SYNC & DIVERS ──
_add_route("/api/metrics", "api_metrics", _pk_sync_api.api_metrics, ["GET"], True)
_add_route("/api/connectivity", "api_connectivity", _pk_sync_api.api_connectivity, ["GET"], True)
_add_route("/api/sync", "api_sync", _pk_sync_api.api_sync, ["POST"], True)
_add_route("/api/articles/sync", "api_articles_sync", _pk_sync_api.api_articles_sync, ["POST"], True)

# ── CONFIGURATION ──
_add_route("/api/db/test", "api_db_test", _pk_config.api_db_test, ["GET"], True)
_add_route("/api/sfec/test", "api_sfec_test", _pk_config.api_sfec_test, ["GET"], True)
_add_route("/api/sfec/certify", "api_sfec_certify", _pk_config.api_sfec_certify, ["POST"], True)
_add_route("/api/sfec/to-monitor", "api_sfec_to_monitor", _pk_config.api_sfec_to_monitor, ["POST"], True)
_add_route("/api/sfec/debug", "api_sfec_debug", _pk_config.api_sfec_debug, ["POST"], True)
_add_route("/api/sfec/validate", "api_sfec_validate", _pk_config.api_sfec_validate, ["POST"], True)
_add_route("/api/sfec/sync", "api_sfec_sync", _pk_config.api_sfec_sync, ["POST"], True)
_add_route("/api/sfec/certified-from-api", "api_sfec_certified_from_api", _pk_config.api_sfec_certified_from_api, ["GET"], True)

# ── ANNUAIRE ──
_add_route("/api/contacts", "api_contacts", _pk_directory.api_contacts, ["GET"], True)

# ── CONFIGURATION ──
_add_route("/api/tax-rates", "api_tax_rates", _pk_config.api_tax_rates, ["GET"], True)

# ── API SYNC & DIVERS ──
_add_route("/api/ledger-accounts", "api_ledger_accounts", _pk_sync_api.api_ledger_accounts, ["GET"], True)

# ── CONFIGURATION ──
_add_route("/api/tables", "api_tables", _pk_config.api_tables, ["GET"], True)

# ── API SYNC & DIVERS ──
_add_route("/api/certified", "api_certified", _pk_sync_api.api_certified, ["GET"], True)
_add_route("/api/certified/list", "api_certified_list", _pk_sync_api.api_certified_list, ["GET"], True)
_add_route("/api/retry-queue", "api_retry_queue", _pk_sync_api.api_retry_queue, ["GET"], True)

# ── CONFIGURATION ──
_add_route("/api/config", "api_get_config", _pk_config.api_get_config, ["GET"], True)
_add_route("/api/config/db", "api_config_db", _pk_config.api_config_db, ["POST"], True)
_add_route("/api/config/sfec", "api_config_sfec", _pk_config.api_config_sfec, ["POST"], True)
_add_route("/api/config/company", "api_config_company", _pk_config.api_config_company, ["POST"], True)
_add_route("/api/config/sync", "api_config_sync", _pk_config.api_config_sync, ["POST"], True)
_add_route("/api/config/validation", "api_config_validation", _pk_config.api_config_validation, ["POST"], True)
_add_route("/api/config/notifications", "api_config_notifications", _pk_config.api_config_notifications, ["POST"], True)
_add_route("/api/config/system", "api_config_system", _pk_config.api_config_system, ["POST"], True)
_add_route("/api/config/dashboard", "api_config_dashboard", _pk_config.api_config_dashboard, ["POST"], True)
_add_route("/api/config/reload", "api_config_reload", _pk_config.api_config_reload, ["POST"], True)
_add_route("/api/config/export", "api_config_export", _pk_config.api_config_export, ["GET"], True)
_add_route("/api/config/import", "api_config_import", _pk_config.api_config_import, ["POST"], True)

# ── FACTURATION ──
_add_route("/billing", "billing_page", _pk_billing.billing_page, ["GET"], True)
_add_route("/api/invoices/list", "api_invoices_list", _pk_billing.api_invoices_list, ["GET"], True)
_add_route("/billing/invoice/new", "invoice_new_page", _pk_billing.invoice_new_page, ["GET"], True)
_add_route("/billing/invoice/<int:invoice_id>", "invoice_detail_page", _pk_billing.invoice_detail_page, ["GET"], True)
_add_route("/billing/invoice/<int:invoice_id>/edit", "invoice_edit_page", _pk_billing.invoice_edit_page, ["GET"], True)
_add_route("/api/invoices", "api_list_invoices", _pk_billing.api_list_invoices, ["GET"], True)
_add_route("/api/invoices", "api_create_invoice", _pk_billing.api_create_invoice, ["POST"], True)
_add_route("/api/invoices/<int:invoice_id>", "api_get_invoice", _pk_billing.api_get_invoice, ["GET"], True)
_add_route("/api/invoices/<int:invoice_id>", "api_update_invoice", _pk_billing.api_update_invoice, ["PUT"], True)
_add_route("/api/invoices/<int:invoice_id>", "api_delete_invoice", _pk_billing.api_delete_invoice, ["DELETE"], True)
_add_route("/api/invoices/<int:invoice_id>/push-sage", "api_invoice_push_sage", _pk_billing.api_invoice_push_sage, ["POST"], True)
_add_route("/api/invoices/stats", "api_invoice_stats", _pk_billing.api_invoice_stats, ["GET"], True)

# ── API SYNC & DIVERS ──
_add_route("/api/sync/bi", "api_sync_bi", _pk_sync_api.api_sync_bi, ["POST"], True)
_add_route("/api/sync/bi/stats", "api_sync_bi_stats", _pk_sync_api.api_sync_bi_stats, ["GET"], True)

# ── ANNUAIRE ──
_add_route("/api/contacts", "api_list_contacts", _pk_directory.api_list_contacts, ["GET"], True)
_add_route("/api/contacts", "api_create_contact", _pk_directory.api_create_contact, ["POST"], True)

# ── POINT DE VENTE ──
_add_route("/api/products", "api_list_products", _pk_pos.api_list_products, ["GET"], True)
_add_route("/api/products", "api_create_product", _pk_pos.api_create_product, ["POST"], True)

# ── CONFIGURATION ──
_add_route("/api/tax-rates/local", "api_local_tax_rates", _pk_config.api_local_tax_rates, ["GET"], True)

# ── POINT DE VENTE ──
_add_route("/pos", "pos_page", _pk_pos.pos_page, ["GET"], True)
_add_route("/pos/ticket/<int:ticket_id>/print", "pos_print_ticket", _pk_pos.pos_print_ticket, ["GET"], True)
_add_route("/api/pos/config", "api_pos_config", _pk_pos.api_pos_config, ["GET", "POST"], True)
_add_route("/api/pos/ticket", "api_create_ticket", _pk_pos.api_create_ticket, ["POST"], True)
_add_route("/api/pos/tickets", "api_list_tickets", _pk_pos.api_list_tickets, ["GET"], True)
_add_route("/api/pos/ticket/<int:ticket_id>", "api_get_ticket", _pk_pos.api_get_ticket, ["GET"], True)
_add_route("/api/pos/stats", "api_pos_stats", _pk_pos.api_pos_stats, ["GET"], True)
_add_route("/api/pos/products/search", "api_search_products", _pk_pos.api_search_products, ["GET"], True)
_add_route("/api/products/barcode", "api_find_barcode", _pk_pos.api_find_barcode, ["GET"], True)

# ── PAGES TABLEAU DE BORD ──
_add_route("/sales", "sales_page", _pk_dashboard_pages.sales_page, ["GET"], True)

# ── ANNUAIRE ──
_add_route("/vendeurs", "vendeurs_page", _pk_directory.vendeurs_page, ["GET"], True)
_add_route("/api/vendeurs", "api_list_vendeurs", _pk_directory.api_list_vendeurs, ["GET"], True)
_add_route("/api/vendeurs", "api_create_vendeur", _pk_directory.api_create_vendeur, ["POST"], True)
_add_route("/api/vendeurs/<int:vendeur_id>", "api_get_vendeur", _pk_directory.api_get_vendeur, ["GET"], True)
_add_route("/api/vendeurs/<int:vendeur_id>", "api_update_vendeur", _pk_directory.api_update_vendeur, ["PUT"], True)
_add_route("/api/vendeurs/<int:vendeur_id>", "api_delete_vendeur", _pk_directory.api_delete_vendeur, ["DELETE"], True)

# ── FACTURATION ──
_add_route("/api/invoices/<int:invoice_id>/pdf", "api_invoice_pdf", _pk_billing.api_invoice_pdf, ["GET"], True)

# ── POINT DE VENTE ──
_add_route("/api/pos/ticket/<int:ticket_id>/pdf", "api_ticket_pdf", _pk_pos.api_ticket_pdf, ["GET"], True)

# ── ANNUAIRE ──
_add_route("/api/contacts/<int:contact_id>", "api_update_contact", _pk_directory.api_update_contact, ["PUT"], True)
_add_route("/api/contacts/<int:contact_id>", "api_delete_contact", _pk_directory.api_delete_contact, ["DELETE"], True)
_add_route("/clients", "clients_page", _pk_directory.clients_page, ["GET"], True)
_add_route("/utilisateurs", "utilisateurs_page", _pk_directory.utilisateurs_page, ["GET"], True)

# ── AUTHENTIFICATION ──
_add_route("/compte/mot-de-passe", "compte_password_page", _pk_auth.compte_password_page, ["GET"], True)

# ── ANNUAIRE ──
_add_route("/api/utilisateurs", "api_list_comptes", _pk_directory.api_list_comptes, ["GET"], True)
_add_route("/api/utilisateurs", "api_create_compte", _pk_directory.api_create_compte, ["POST"], True)
_add_route("/api/utilisateurs/<int:user_id>", "api_update_compte", _pk_directory.api_update_compte, ["PUT"], True)
_add_route("/api/utilisateurs/<int:user_id>", "api_delete_compte", _pk_directory.api_delete_compte, ["DELETE"], True)
_add_route("/api/utilisateurs/<int:user_id>/password", "api_reset_compte_password", _pk_directory.api_reset_compte_password, ["POST"], True)

# ── AUTHENTIFICATION ──
_add_route("/api/compte/password", "api_compte_password", _pk_auth.api_compte_password, ["POST"], True)

# ── API SYNC & DIVERS ──
_add_route("/api/auth/permissions", "api_auth_permissions", _pk_sync_api.api_auth_permissions, ["GET", "POST"], True)
_add_route("/api/auth/config", "api_auth_config", _pk_sync_api.api_auth_config, ["GET", "POST"], True)
_add_route("/api/csrf", "api_csrf", _pk_sync_api.api_csrf, ["GET"], False)
_add_route("/api/audit", "api_audit", _pk_sync_api.api_audit, ["GET"], True)

# ── PAGES TABLEAU DE BORD ──
_add_route("/health", "health", _pk_dashboard_pages.health, ["GET"], False)
_add_route("/ready", "ready", _pk_dashboard_pages.ready, ["GET"], False)

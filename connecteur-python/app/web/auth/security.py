"""Sécurité web — hooks Flask, CSRF, authentification, permissions.

Déplacé depuis app/web/dashboard.py (étape A7). Fonctions pures et
utilitaires importables SANS import circulaire : ce module ne dépend que
de Flask, de la config et de app.web.auth.user_auth. La factory
(app/web/app.py) appelle `init_security(app)` pour brancher les hooks
before/after_request ; les blueprints importent directement `_login_required`,
`_current_identity`, `_get_csrf_token`, etc.
"""
import functools
import hmac
import html
import logging
import secrets
from datetime import timedelta

from flask import (
    current_app, jsonify, redirect, render_template, request, session, url_for,
)

from app.config.manager import get_config
from app.web.auth import user_auth


logger = logging.getLogger("t-connector.dashboard")

# Sentinelles internes de résolution de permission.
_PERM_ADMIN = "@admin"
_PERM_AUTH = "@auth"
_PERM_DENY = "@deny"

# Un secret absent, court ou laissé à une valeur d'exemple ne doit jamais
# servir de clé de signature : sinon toute installation fraîche partage la
# même clé (forge de cookies). On génère alors un secret aléatoire par
# processus ; il est préférable de définir dashboard.session_secret.
_INSECURE_SECRETS = {
    "", "CHANGE-ME", "CHANGE-ME-IN-PRODUCTION", "change-me", "secret",
}
_GENERATED_SECRET = secrets.token_hex(32)


def _esc(val):
    if val is None:
        return ""
    return html.escape(str(val))


def _get_secret_key():
    cfg = get_config().get("dashboard", {})
    key = str(cfg.get("session_secret", "") or "")
    if key in _INSECURE_SECRETS or len(key) < 32:
        logger.warning(
            "dashboard.session_secret absent/faible : utilisation d'un secret "
            "aleatoire ephemere. Definissez une valeur d'au moins 32 caracteres."
        )
        return _GENERATED_SECRET
    return key


def _auth_enabled():
    return get_config().get("dashboard", {}).get("auth_enabled", True)


def _apply_session_security(app=None):
    """Applique la config de durcissement des cookies de session.

    Idempotent ; utiliser `current_app` si aucune app n'est passée (contexte
    de requête). Appelée par init_security() à la construction et par le
    hook before_request (équivalent strict du comportement historique).
    """
    target = app if app is not None else current_app
    from app.web.auth.user_auth import get_auth_config
    acfg = get_auth_config()
    target.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=acfg.get("https_only", False),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=acfg.get("session_max_age_hours", 24)),
        SESSION_REFRESH_EACH_REQUEST=True,
    )


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

_PUBLIC_PATHS = {
    "/login", "/logout", "/health", "/ready",
    "/api/connectivity", "/api/csrf",
}
_PUBLIC_PREFIXES = ("/static/",)


def _is_public_path(path):
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


def _route_path(path):
    """Chemin normalisé (sans slash final) pour les règles de permission."""
    return path.rstrip("/") or "/"


def _required_permission(path, method):
    """Résout la permission d'une route CONNUE.

    Fail-closed : toute route non explicitement couverte retourne _PERM_DENY.
    Les routes publiques et les 404 sont traitées dans _auth_before_request.
    """
    role = session.get("user_role") or ""
    if role == "admin":
        return _PERM_ADMIN, None

    p = _route_path(path).lower()
    m = (method or "GET").upper()

    # Compte : accessible à tout utilisateur authentifié.
    if p in ("/compte/mot-de-passe", "/api/compte/password"):
        return _PERM_AUTH, None

    # Configuration / diagnostic technique.
    if (p in ("/config", "/api/config", "/api/db/test", "/api/tables",
              "/api/tax-rates", "/api/tax-rates/local")
            or p.startswith("/api/config/")):
        return "config.gerer", None

    # Comptes, matrice de droits, audit.
    if (p == "/utilisateurs" or p.startswith("/api/utilisateurs")
            or p == "/api/audit" or p.startswith("/api/auth/")):
        return "utilisateurs.gerer", None

    # Facturation : pages et APIs.
    if p == "/billing/invoice/new" or (
            p.startswith("/billing/invoice/") and p.endswith("/edit")):
        return "factures.creer", None
    if p == "/billing" or p.startswith("/billing/invoice/"):
        return "factures.voir", None
    if (p in ("/invoices", "/pending", "/certified", "/sales")
            or p.startswith("/certified/")):
        return "factures.voir", None
    if p.startswith("/api/invoices"):
        if p.endswith("/push-sage") and m == "POST":
            return "sage.pousser", None
        if p == "/api/invoices" and m == "POST":
            return "factures.creer", None
        if m in ("PUT", "PATCH"):
            return "factures.creer", None
        if m == "DELETE":
            return "factures.supprimer", None
        return "factures.voir", None
    if (p.startswith("/api/certified") or p == "/api/retry-queue"
            or p == "/api/ledger-accounts"):
        return "factures.voir", None

    # POS : le controle global du stock releve de la configuration.
    if p == "/api/pos/config":
        return "config.gerer", None

    # POS.
    if (p == "/pos" or p.startswith("/pos/") or p.startswith("/api/pos")
            or p.startswith("/api/products")):
        return "pos.vente", None
    if p == "/api/articles/sync":
        return "sage.sync", None

    # Annuaire : lecture vs mutation.
    mutate = m in ("POST", "PUT", "PATCH", "DELETE")
    if p == "/clients" or p == "/api/contacts/sage":
        return "clients.voir", None
    if p.startswith("/api/contacts"):
        return "clients.gerer" if mutate else "clients.voir", None
    if p == "/vendeurs":
        return "vendeurs.voir", None
    if p.startswith("/api/vendeurs"):
        return "vendeurs.gerer" if mutate else "vendeurs.voir", None

    # Dashboard / synchronisation / SFEC.
    if p == "/" or p == "/api/metrics":
        return "dashboard.voir", None
    if p.startswith("/api/sync"):
        return "sage.sync", None
    if p.startswith("/api/sfec"):
        return "sfec.certifier", None

    return _PERM_DENY, None


def _deny(perm, label):
    if request.path.startswith("/api/"):
        payload = {"error": "Acces refuse (droits insuffisants)"}
        if perm and not str(perm).startswith("@"):
            payload["permission"] = perm
        return jsonify(payload), 403
    return render_template("auth/deny.html",
                           label=_esc(label or "Acces non autorise"),
                           email=_esc(session.get("user_email", ""))), 403


def _auth_before_request():
    if not _auth_enabled():
        return None
    _apply_session_security()
    path = request.path

    # Rafraîchit systématiquement le rôle depuis la base : une rétrogradation
    # ou une désactivation prend effet immédiatement (audit AU-06).
    if session.get("user_id"):
        live = None
        try:
            live = user_auth.get_user(session.get("user_id"))
        except Exception:
            pass
        if not live or not live.get("est_actif"):
            session.clear()
            if path.startswith("/api/"):
                return jsonify({"error": "Non authentifie"}), 401
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

    if _is_public_path(path):
        return None
    if not session.get("user_id"):
        if path.startswith("/api/"):
            return jsonify({"error": "Non authentifie"}), 401
        return redirect(url_for("auth.login_page"))

    # Route inconnue : laisser Flask produire un 404, sans appliquer le
    # fail-closed (sinon toutes les URLs inexistantes répondraient 403).
    if request.url_rule is None:
        return None

    perm, _m = _required_permission(path, request.method)
    if perm in (_PERM_ADMIN, _PERM_AUTH):
        return None
    if perm == _PERM_DENY or not perm:
        return _deny(None, "Acces non autorise")
    if not user_auth.has_perm(session.get("user_role", ""), perm):
        return _deny(perm, user_auth.PERMISSIONS.get(perm, perm))
    return None


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


def init_security(app):
    """Branche la sécurité sur l'application Flask (appelé par create_app).

    Applique la config de session, puis enregistre les hooks before/after_request.
    """
    _apply_session_security(app)
    app.before_request(_auth_before_request)
    app.after_request(_security_headers)

"""Blueprint auth — Authentification : /login /logout /compte/mot-de-passe
/api/compte/password.

Migré depuis app/web/parking/auth.py (étape A4). Les handlers sont
strictement inchangés ; les URL / méthodes HTTP sont identiques aux
app.add_url_rule() historiques de app/web/dashboard.py. Le wrapping
@_login_required est reproduit à l'identique (liaison tardive en bas de
module, même convention que les parkings — aucun import circulaire
possible quel que soit l'ordre d'import).
"""

import functools
import secrets
from datetime import timedelta
from flask import Blueprint, jsonify, redirect, render_template, request, session
from app.storage import db as sqlite_db
from app.web.auth import user_auth
from app.web.parking.common import _esc, _page


bp = Blueprint('auth', __name__)


def __getattr__(name):
    """Filet de sécurité : délégation vers app.web.dashboard pour tout nom
    non résolu (uniquement effectif sur accès attribut du module). Les noms
    réellement utilisés par les handlers sont liés explicitement en bas de
    ce module (voir section « Liaison dashboard »)."""
    from app.web import dashboard as _dashboard
    return getattr(_dashboard, name)


def _login_required_late(f):
    """Reproduit le wrapping historique view_func = _login_required(view_func).
    _login_required est lié en bas de module (liaison tardive) : le wrapper
    ne le résout qu'à l'exécution, donc aucun import circulaire à l'import."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return _login_required(f)(*args, **kwargs)
    return wrapper


def _login_csrf_field():
    return ('<input type="hidden" name="_csrf" value="{}">').format(_esc(_get_csrf_token()))


@bp.route("/login", methods=["GET", "POST"])
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


@bp.route("/logout", methods=["GET"])
def logout():
    user_auth.audit("deconnexion", "Deconnexion", email=session.get("user_email", ""),
                    ip=request.remote_addr or "")
    session.clear()
    return redirect("/login")


@bp.route("/compte/mot-de-passe", methods=["GET"])
@_login_required_late
def compte_password_page():
    return _page(render_template("auth/password.html"))


@bp.route("/api/compte/password", methods=["POST"])
@_login_required_late
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


# ── Liaison dashboard ──
# Ces noms sont définis dans app/web/dashboard.py. On les lie ICI, en bas de
# module : quel que soit l'ordre d'import (dashboard d'abord ou routes
# d'abord), ils existent déjà dans le namespace de dashboard.py à ce stade —
# aucun import circulaire possible. Les corps des fonctions ci-dessus restent
# strictement inchangés (références globales résolues à l'exécution).
from app.web import dashboard as _dashboard
_login_required = _dashboard._login_required
_apply_session_security = _dashboard._apply_session_security
_auth_enabled = _dashboard._auth_enabled
_csrf_valid = _dashboard._csrf_valid
_current_identity = _dashboard._current_identity
_get_csrf_token = _dashboard._get_csrf_token
app = _dashboard.app

del _dashboard

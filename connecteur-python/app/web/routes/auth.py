"""Blueprint auth — Authentification : /login /logout /compte/mot-de-passe
/api/compte/password.

Migré depuis app/web/parking/auth.py (étape A4). Les handlers sont
strictement inchangés ; les URL / méthodes HTTP sont identiques aux
app.add_url_rule() historiques de app/web/dashboard.py. Le wrapping @_login_required est reproduit à l'identique, désormais via
un import direct de app.web.auth.security (aucun import circulaire : la
sécurité ne dépend d'aucun module de routes).
"""

import functools
import secrets
from datetime import timedelta
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session
from app.storage import db as sqlite_db
from app.web.auth import user_auth
from app.web.auth.security import _login_required, _apply_session_security, _auth_enabled, _csrf_valid, _current_identity, _get_csrf_token, _is_sage_connected_user
from app.web.common import _esc, _page


bp = Blueprint('auth', __name__)


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
    session["_pwd_ver"] = user.get("session_version", 0)
    session["_must_change_pwd"] = bool(user.get("must_change_pwd"))
    import time as _time
    session["_last_activity"] = _time.time()
    session.permanent = True
    current_app.permanent_session_lifetime = timedelta(hours=user_auth.get_auth_config().get("session_max_age_hours", 24))
    session["_csrf"] = secrets.token_urlsafe(32)

    email_clean = user["email"]
    session["vendeur_id"] = None
    try:
        with sqlite_db.get_cursor() as cur:
            # Comparaison insensible a la casse (AU-07) : un email saisi avec
            # une casse differente dans l'annuaire vendeurs reste lie.
            cur.execute("SELECT id FROM vendeurs WHERE lower(email) = lower(?) AND est_actif = 1 LIMIT 1",
                        (email_clean,))
            row = cur.fetchone()
            session["vendeur_id"] = row["id"] if row else None
    except Exception:
        session["vendeur_id"] = None
    return redirect("/")


@bp.route("/logout", methods=["POST"])
def logout():
    user_auth.audit("deconnexion", "Deconnexion", email=session.get("user_email", ""),
                    ip=request.remote_addr or "")
    session.clear()
    return redirect("/login")


@bp.route("/compte/mot-de-passe", methods=["GET"])
@_login_required
def compte_password_page():
    return _page(render_template("auth/password.html"))


@bp.route("/api/compte/password", methods=["POST"])
@_login_required
def api_compte_password():
    ident = _current_identity() or {}
    data = request.get_json(silent=True) or {}
    if _is_sage_connected_user():
        # En mode provider=sage, le mot de passe de reference est gere par
        # Sage : ecrire un hash local creerait deux secrets divergents (AU-15).
        return jsonify({"success": False,
                        "error": "Mot de passe gere par Sage : changement local desactive"}), 400
    try:
        user, err = user_auth.authenticate(
            ident.get("email", ""), data.get("current", ""),
            ip=request.remote_addr or "")
        if not user:
            return jsonify({"success": False, "error": "Mot de passe actuel incorrect"}), 400
        user_auth.set_password(user["id"], data.get("nouveau", ""))
        # La session courante reste valide : on la re-synchronise avec la
        # nouvelle version (les AUTRES sessions du compte sont revoquees).
        fresh = user_auth.get_user(user["id"]) or {}
        session["_pwd_ver"] = fresh.get("session_version", 0)
        session["_must_change_pwd"] = bool(fresh.get("must_change_pwd"))
        # Renouvellement du jeton CSRF (AU-18) : le frontend le recuperera
        # via /api/csrf a la prochaine requete.
        session["_csrf"] = secrets.token_urlsafe(32)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400




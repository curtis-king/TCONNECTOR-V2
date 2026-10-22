"""Tests de non-regression securite (audit AUTH / CONFIG).

Couvre :
  - RBAC fail-closed et matrice par role ;
  - rafraichissement immediat du role depuis la base ;
  - throttle email+IP sans verrouillage global du compte ;
  - secrets jamais rendus par /config, /api/config, export ;
  - refus de desactivation de l'authentification ;
  - CSRF et logout POST-only ;
  - echappement XSS de l'annuaire et du POS.
"""
import json
import re
import uuid

import pytest

from app.config.manager import get_config
from app.storage.db import get_cursor
from app.web.auth import user_auth

PREFIX = "TEST-SEC-"


def _login(app_instance, email, password):
    client = app_instance.test_client()
    page = client.get("/login")
    match = re.search(r'name="_csrf" value="([^"]+)"', page.get_data(as_text=True))
    assert match, "Jeton CSRF de /login introuvable"
    resp = client.post("/login", data={
        "email": email, "password": password, "_csrf": match.group(1),
    })
    assert resp.status_code in (302, 303), resp.status_code
    return client


def _csrf(client):
    return client.get("/api/csrf").get_json()["token"]


def _make_user(role, password="Test-Password-123"):
    email = "{}{}@example.com".format(PREFIX.lower(), uuid.uuid4().hex[:12])
    user_auth.create_user(
        nom="Securite", prenom="Test", email=email,
        password=password, role=role,
    )
    return email, password


def _purge_user(email):
    with get_cursor() as cur:
        cur.execute("DELETE FROM audit_log WHERE lower(utilisateur) = lower(?)", (email,))
        cur.execute("DELETE FROM utilisateurs WHERE lower(email) = lower(?)", (email,))


def _purge_contact(code):
    with get_cursor() as cur:
        cur.execute("DELETE FROM contacts WHERE code = ?", (code,))


@pytest.fixture
def temp_user():
    created = []

    def _create(role):
        email, password = _make_user(role)
        created.append(email)
        return email, password

    yield _create
    for email in created:
        _purge_user(email)


# ── RBAC ────────────────────────────────────────────────────────────────────

def test_anonymous_api_and_page_are_protected(anonymous_client):
    assert anonymous_client.get("/api/metrics").status_code == 401
    assert anonymous_client.get("/").status_code == 302


def test_caissiere_cannot_reach_config_or_mutate_invoices(role_client):
    client = role_client("caissiere")
    token = _csrf(client)

    assert client.get("/pos").status_code == 200
    assert client.get("/api/metrics").status_code == 200
    assert client.get("/config").status_code == 403
    assert client.get("/api/config").status_code == 403
    assert client.get("/utilisateurs").status_code == 403
    assert client.post("/api/invoices", json={}, headers={
        "X-CSRF-Token": token,
    }).status_code == 403
    assert client.post("/api/contacts", json={}, headers={
        "X-CSRF-Token": token,
    }).status_code == 403


def test_financiere_can_open_billing_but_not_pos_or_config(role_client):
    client = role_client("financiere")
    token = _csrf(client)

    assert client.get("/billing").status_code == 200
    assert client.get("/pos").status_code == 403
    assert client.get("/config").status_code == 403
    # L'ecriture est autorisee mais le payload avoir invalide doit etre rejete
    # par la validation metier, jamais par le RBAC (403).
    resp = client.post("/api/invoices", json={"type_doc": "avoir"}, headers={
        "X-CSRF-Token": token,
    })
    assert resp.status_code == 422, resp.get_json()


def test_role_change_is_applied_on_next_request(role_client):
    client = role_client("financiere")
    assert client.get("/pos").status_code == 403

    user = user_auth.get_user_by_email("test-financiere@example.com")
    user_auth.update_user(user["id"], role="caissiere")

    assert client.get("/pos").status_code == 200


# ── Authentification / brute force ──────────────────────────────────────────

def test_lockout_is_per_email_and_ip_not_global(temp_user):
    email, password = temp_user("caissiere")

    for _ in range(5):
        user, err = user_auth.authenticate(email, "mauvais", ip="10.0.0.1")
        assert user is None
        assert err == "Identifiants incorrects"

    user, err = user_auth.authenticate(email, password, ip="10.0.0.1")
    assert user is None
    assert err == "Identifiants incorrects"

    # Une autre IP n'est pas bloquee : pas de DoS global du compte.
    user, err = user_auth.authenticate(email, password, ip="10.0.0.2")
    assert err is None
    assert user and user["email"] == email


def test_disabled_account_uses_generic_error(temp_user):
    email, password = temp_user("caissiere")
    with get_cursor() as cur:
        cur.execute("UPDATE utilisateurs SET est_actif = 0 WHERE lower(email) = lower(?)",
                    (email,))

    user, err = user_auth.authenticate(email, password, ip="10.0.0.3")
    assert user is None
    assert err == "Identifiants incorrects"


# ── Secrets ─────────────────────────────────────────────────────────────────

def _secret_values():
    cfg = get_config()
    return {
        ("db", "password"): cfg.get("db", {}).get("password", ""),
        ("sfec", "api_key"): cfg.get("sfec", {}).get("api_key", ""),
        ("dashboard", "login_password"): cfg.get("dashboard", {}).get("login_password", ""),
        ("dashboard", "session_secret"): cfg.get("dashboard", {}).get("session_secret", ""),
    }


def test_config_page_and_api_do_not_render_secrets(client):
    html = client.get("/config").get_data(as_text=True)
    for (section, key), value in _secret_values().items():
        if value:
            assert value not in html, "{}:{} expose dans /config".format(section, key)

    body = client.get("/api/config").get_json()
    for (section, key), value in _secret_values().items():
        if value:
            assert body[section][key] == "***", "{}:{} non masque".format(section, key)


def test_config_export_redacts_secrets(client):
    resp = client.get("/api/config/export")
    assert resp.status_code == 200
    exported = json.loads(resp.get_data(as_text=True))
    for (section, key), value in _secret_values().items():
        if value:
            assert exported[section][key] == "***"


def test_auth_cannot_be_disabled_via_api(client):
    resp = client.post("/api/config/system", json={"auth_enabled": False},
                       headers={"X-CSRF-Token": _csrf(client)})
    assert resp.status_code == 400
    assert get_config().get("dashboard", {}).get("auth_enabled", True) is True


# ── CSRF / logout ───────────────────────────────────────────────────────────

def test_logout_is_post_only_and_requires_csrf(app_instance, admin_credentials):
    client = _login(app_instance, admin_credentials["email"], admin_credentials["password"])
    token = _csrf(client)

    assert client.get("/logout").status_code == 405
    assert client.post("/logout").status_code == 400
    assert client.get("/api/metrics").status_code == 200

    assert client.post("/logout", headers={"X-CSRF-Token": token}).status_code == 302
    assert client.get("/api/metrics").status_code == 401


# ── XSS ─────────────────────────────────────────────────────────────────────

def test_directory_and_pos_escape_contact_fields(client):
    code = PREFIX + "XSS"
    payload = "<script>alert('xss')</script>"
    _purge_contact(code)
    token = _csrf(client)
    try:
        resp = client.post("/api/contacts", json={
            "code": code, "nom": payload, "type": "client",
        }, headers={"X-CSRF-Token": token})
        assert resp.status_code == 201, resp.get_json()

        directory = client.get("/clients").get_data(as_text=True)
        assert payload not in directory
        assert "&lt;script&gt;" in directory

        pos = client.get("/pos").get_data(as_text=True)
        assert payload not in pos
    finally:
        _purge_contact(code)


# ── Sessions & mots de passe (AU-13/17/18/15/26/27/31/07) ──────────────────

def test_password_change_revokes_other_sessions(app_instance, temp_user):
    """AU-13 : apres changement de mot de passe, les autres sessions du compte
    sont revoquees ; la session qui a fait le changement reste valide."""
    email, password = temp_user("responsable")
    c1 = _login(app_instance, email, password)
    c2 = _login(app_instance, email, password)
    assert c2.get("/api/metrics").status_code == 200

    token = _csrf(c1)
    r = c1.post("/api/compte/password", json={
        "current": password, "nouveau": "Nouveau-MDP-456",
    }, headers={"X-CSRF-Token": token})
    assert r.status_code == 200, r.get_json()

    # c2 (ancienne session) est revoquee
    assert c2.get("/api/metrics").status_code == 401
    # c1 (session du changement) reste valide
    t2 = _csrf(c1)
    assert c1.get("/api/metrics").status_code == 200
    assert t2, "un nouveau jeton CSRF doit etre disponible"


def test_password_change_rotates_csrf_token(app_instance, temp_user):
    """AU-18 : le jeton CSRF d'avant le changement est refuse ensuite."""
    email, password = temp_user("responsable")
    c = _login(app_instance, email, password)
    old_token = _csrf(c)
    r = c.post("/api/compte/password", json={
        "current": password, "nouveau": "Autre-MDP-789",
    }, headers={"X-CSRF-Token": old_token})
    assert r.status_code == 200, r.get_json()

    new_token = _csrf(c)
    assert new_token != old_token
    # L'ancien jeton est rejete par le hook global (400).
    r2 = c.post("/api/compte/password", json={
        "current": "Autre-MDP-789", "nouveau": "Encore-MDP-000",
    }, headers={"X-CSRF-Token": old_token})
    assert r2.status_code == 400
    # Le nouveau fonctionne.
    r3 = c.post("/api/compte/password", json={
        "current": "Autre-MDP-789", "nouveau": "Encore-MDP-000",
    }, headers={"X-CSRF-Token": new_token})
    assert r3.status_code == 200, r3.get_json()


def test_admin_reset_forces_password_change(app_instance, client, temp_user):
    """AU-17 : un mot de passe reinitialise par un admin impose un changement
    a la prochaine connexion ; seul /compte/mot-de-passe reste accessible."""
    email, _old = temp_user("responsable")
    with get_cursor() as cur:
        cur.execute("SELECT id FROM utilisateurs WHERE lower(email) = lower(?)", (email,))
        uid = cur.fetchone()["id"]

    token = _csrf(client)
    r = client.post("/api/utilisateurs/{}/password".format(uid), json={
        "password": "Temporaire-123",
    }, headers={"X-CSRF-Token": token})
    assert r.status_code == 200, r.get_json()

    c = _login(app_instance, email, "Temporaire-123")
    # Toute page autre que le changement de mot de passe redirige.
    r = c.get("/")
    assert r.status_code in (302, 303)
    assert "/compte/mot-de-passe" in (r.headers.get("Location") or "")
    # Les APIs renvoient 403 explicite.
    r = c.get("/api/metrics")
    assert r.status_code == 403
    assert r.get_json().get("must_change_password") is True
    # La page de changement reste accessible.
    assert c.get("/compte/mot-de-passe").status_code == 200
    # Apres changement effectif, acces normal.
    token = _csrf(c)
    r = c.post("/api/compte/password", json={
        "current": "Temporaire-123", "nouveau": "Definitif-456",
    }, headers={"X-CSRF-Token": token})
    assert r.status_code == 200, r.get_json()
    assert c.get("/api/metrics").status_code == 200


def test_sage_provider_blocks_local_password_change(app_instance, temp_user):
    """AU-15 : en mode provider=sage, le changement local est refuse."""
    from app.config.manager import get_config, save_config
    email, password = temp_user("responsable")
    c = _login(app_instance, email, password)
    cfg = get_config()
    old_provider = (cfg.get("auth") or {}).get("provider", "local")
    try:
        cfg.setdefault("auth", {})["provider"] = "sage"
        save_config(cfg)
        token = _csrf(c)
        r = c.post("/api/compte/password", json={
            "current": password, "nouveau": "Inutile-MDP-123",
        }, headers={"X-CSRF-Token": token})
        assert r.status_code == 400
        assert "Sage" in (r.get_json().get("error") or "")
    finally:
        cfg.setdefault("auth", {})["provider"] = old_provider
        save_config(cfg)


def test_deny_is_audited(role_client):
    """AU-26 : un refus de permission ecrit dans audit_log."""
    caissiere = role_client("caissiere")
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'acces.refuse'")
        before = cur.fetchone()["n"]
    assert caissiere.get("/config").status_code == 403
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'acces.refuse'")
        after = cur.fetchone()["n"]
        cur.execute(
            "SELECT detail FROM audit_log WHERE action = 'acces.refuse' ORDER BY id DESC LIMIT 1")
        last = cur.fetchone()["detail"]
    assert after == before + 1
    assert "/config" in last


def test_matrix_cache_is_invalidated_on_write():
    """AU-27 : get_matrix() est mis en cache, mais set_matrix() invalide."""
    role = "caissiere"
    original = user_auth.role_permissions(role)
    try:
        matrix = user_auth.get_matrix()
        perms = [p for p in matrix.get(role, []) if p != "config.gerer"]
        matrix[role] = perms
        user_auth.set_matrix(matrix)
        assert not user_auth.has_perm(role, "config.gerer")
    finally:
        user_auth.set_matrix({r: sorted(user_auth.DEFAULT_MATRIX[r]) for r in user_auth.ROLES})
    assert user_auth.role_permissions(role) == original


def test_hsts_only_when_https_only(app_instance):
    """AU-31 : HSTS present quand https_only est actif, absent sinon."""
    from app.config.manager import get_config, save_config
    cfg = get_config()
    old = bool((cfg.get("auth") or {}).get("https_only", False))
    c = app_instance.test_client()
    try:
        cfg.setdefault("auth", {})["https_only"] = True
        save_config(cfg)
        assert "Strict-Transport-Security" in c.get("/login").headers

        cfg.setdefault("auth", {})["https_only"] = False
        save_config(cfg)
        assert "Strict-Transport-Security" not in c.get("/login").headers
    finally:
        cfg.setdefault("auth", {})["https_only"] = old
        save_config(cfg)


def test_vendeur_link_is_case_insensitive(app_instance):
    """AU-07 : la liaison compte<->vendeur par email ignore la casse."""
    from app.storage.db import get_cursor as _cur
    email = "test-sec-vendeur@example.com"
    user_auth.create_user(nom="Vendeur", prenom="Casse", email=email,
                          password="Test-Password-123", role="caissiere")
    with _cur() as cur:
        cur.execute(
            "INSERT INTO vendeurs (code, nom, prenom, email, pin, role, est_actif)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)",
            ("TEST-SEC-V1", "Casse", "Vendeur", email.upper(), "", "vendeur"))
    try:
        c = _login(app_instance, email, "Test-Password-123")
        with c.session_transaction() as s:
            assert s.get("vendeur_id"), "vendeur_id devrait etre lie malgre la casse"
    finally:
        _purge_user(email)
        with _cur() as cur:
            cur.execute("DELETE FROM vendeurs WHERE code = ?", ("TEST-SEC-V1",))


# ── Validation des plages de configuration (CF-13/14/15/22/23) ───────────────

def test_config_db_rejette_valeurs_hors_plage(client):
    """CF-13/23 : port invalide et pool_min > pool_max sont refuses (400)."""
    token = _csrf(client)
    r = client.post("/api/config/db", json={"port": 99999},
                    headers={"X-CSRF-Token": token})
    assert r.status_code == 400
    assert "port" in str(r.get_json())

    from app.config.manager import get_config
    pool_max = int((get_config().get("db") or {}).get("pool_max", 10) or 10)
    r = client.post("/api/config/db", json={"pool_min": pool_max + 5},
                    headers={"X-CSRF-Token": token})
    assert r.status_code == 400
    assert "pool_min" in str(r.get_json())


def test_config_sfec_valide_urls_et_statut(client):
    """CF-14/15 : URL non-http(s) refusee, hote prive signale (non bloque),
    certify_status restreint a la liste SFEC."""
    from app.config.manager import get_config, save_config
    token = _csrf(client)
    cfg = get_config()
    old_urls = {k: (cfg.get("sfec") or {}).get(k, "") for k in ("base_url", "sandbox_url")}
    try:
        r = client.post("/api/config/sfec", json={"base_url": "ftp://x"},
                        headers={"X-CSRF-Token": token})
        assert r.status_code == 400

        r = client.post("/api/config/sfec", json={"base_url": "http://192.168.10.5:8080"},
                        headers={"X-CSRF-Token": token})
        assert r.status_code == 200
        assert r.get_json().get("warnings"), "un avertissement hote prive est attendu"

        r = client.post("/api/config/sfec", json={"certify_status": "BIDON"},
                        headers={"X-CSRF-Token": token})
        assert r.status_code == 400
    finally:
        cfg = get_config()
        cfg.setdefault("sfec", {}).update(old_urls)
        save_config(cfg)


def test_config_validation_controle_croise(client):
    """CF-22 : tolerance negative refusee, per_minute_max <= daily_max impose."""
    from app.config.manager import get_config
    token = _csrf(client)
    daily = int((get_config().get("rate_limit") or {}).get("daily_max", 1000) or 1000)
    r = client.post("/api/config/validation", json={"tolerance_amount": -5},
                    headers={"X-CSRF-Token": token})
    assert r.status_code == 400
    r = client.post("/api/config/validation", json={"per_minute_max": daily + 1},
                    headers={"X-CSRF-Token": token})
    assert r.status_code == 400
    assert "per_minute_max" in str(r.get_json())


def test_config_sync_borne_file_attente(client):
    """CF-13 : job_timeout_ms negatif refuse sur /api/config/sync."""
    token = _csrf(client)
    r = client.post("/api/config/sync", json={"job_timeout_ms": -1},
                    headers={"X-CSRF-Token": token})
    assert r.status_code == 400

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
